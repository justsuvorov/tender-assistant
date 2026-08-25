import re
from typing import List


class TokenEstimator:
    """Грубая оценка числа токенов.

    Для русского текста один токен ≈ 2.5 символа. Точность здесь не важна:
    оценка нужна только чтобы решить, влезает ли база знаний в контекст.
    """

    CHARS_PER_TOKEN = 2.5

    @classmethod
    def tokens(cls, text: str) -> int:
        return int(len(text) / cls.CHARS_PER_TOKEN) + 1

    @classmethod
    def chars(cls, tokens: int) -> int:
        return int(tokens * cls.CHARS_PER_TOKEN)


class NormativeIndex:
    """Разбивает нормативную базу на разделы и ищет среди них релевантные.

    Поиск — по пересечению слов запроса и раздела (BM25-подобная эвристика без
    внешних зависимостей). Этого достаточно, чтобы отобрать нужные разделы,
    когда база не влезает в контекст модели целиком.
    """

    _SECTION_SPLIT = re.compile(r"\n(?=#{1,4}\s)|\n-{3,}\n")
    _WORD = re.compile(r"\w+", re.UNICODE)

    _STOPWORDS = {
        "для", "или", "как", "что", "это", "при", "над", "под", "the", "and",
        "быть", "если", "так", "все", "его", "она", "они", "тот", "чем",
    }

    def __init__(self, text: str):
        self._text = text or ""
        self._sections = self._split(self._text)
        self._section_words = [self._words(s) for s in self._sections]

    @property
    def section_count(self) -> int:
        return len(self._sections)

    @property
    def full_text(self) -> str:
        return self._text

    def relevant(self, query: str, char_budget: int) -> str:
        """Возвращает наиболее релевантные разделы, влезающие в char_budget."""
        if not self._sections or char_budget <= 0:
            return ""

        query_words = self._words(query)
        scored = []
        for idx, words in enumerate(self._section_words):
            if not words:
                continue
            overlap = len(query_words & words)
            score = overlap / (len(words) ** 0.5) if overlap else 0.0
            scored.append((score, idx))

        # Разделы без пересечения тоже могут пригодиться — берём их в конце
        # в исходном порядке, чтобы заполнить остаток бюджета.
        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        chosen: List[int] = []
        used = 0
        for score, idx in scored:
            length = len(self._sections[idx]) + 4
            if used + length > char_budget:
                continue
            chosen.append(idx)
            used += length

        if not chosen:
            return self._sections[0][:char_budget]

        chosen.sort()
        return "\n\n".join(self._sections[i] for i in chosen)

    def _split(self, text: str) -> List[str]:
        if not text.strip():
            return []
        parts = [p.strip() for p in self._SECTION_SPLIT.split(text)]
        return [p for p in parts if p]

    @classmethod
    def _words(cls, text: str) -> set:
        return {
            w.lower()
            for w in cls._WORD.findall(text or "")
            if len(w) > 3 and w.lower() not in cls._STOPWORDS
        }


class ContextBuilder:
    """Собирает промпт, укладываясь в контекстное окно модели.

    Если ``num_ctx <= 0``, ограничение не применяется и база подставляется
    целиком. Иначе под нормативную базу отводится остаток окна после каркаса
    промпта, роли, примеров и исходного текста, минус резерв под ответ.
    """

    ANSWER_RESERVE_TOKENS = 4096
    MIN_BASE_TOKENS = 512

    def __init__(self, num_ctx: int, normative_index: NormativeIndex):
        self._num_ctx = num_ctx
        self._index = normative_index

    def build(self, template: str, role: str, examples: str, source_text: str) -> str:
        skeleton = template.format(
            role=role,
            normative_base="",
            examples=examples,
            source_text=source_text,
        )

        normative = self._fit_normative(skeleton, source_text)

        return template.format(
            role=role,
            normative_base=normative,
            examples=examples,
            source_text=source_text,
        )

    def _fit_normative(self, skeleton: str, source_text: str) -> str:
        full = self._index.full_text
        if not full:
            return ""

        if self._num_ctx <= 0:
            return full

        budget = (
            self._num_ctx
            - self.ANSWER_RESERVE_TOKENS
            - TokenEstimator.tokens(skeleton)
        )

        if budget < self.MIN_BASE_TOKENS:
            print(
                "[WARN] Контекста не хватает на нормативную базу — "
                "она будет опущена",
                flush=True,
            )
            return ""

        if TokenEstimator.tokens(full) <= budget:
            return full

        print(
            f"[INFO] Нормативная база не влезает в контекст, "
            f"отбираем релевантные разделы (бюджет {budget} токенов)",
            flush=True,
        )
        return self._index.relevant(source_text, TokenEstimator.chars(budget))
