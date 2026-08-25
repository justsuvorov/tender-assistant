class PromptEngine:
    def __init__(self, role: str, template: str):
        """
        """
        self._role = role
        self._template = template

    def build(self, source_text: str, context: list[str], **extra_params) -> str:
        """
        Наполняет шаблон данными.
        """

        examples_block = ""
        if context:
            examples_block = "\n\n".join(
                [f"ЭТАЛОН {i+1}:\n{t}" for i, t in enumerate(context)]
            )

        extra_instructions = ""
        if extra_params:
            extra_instructions = "\n".join(
                [f"- {k}: {v}" for k, v in extra_params.items()]
            )

        try:
            return self._template.format(
                role=self._role,
                examples=examples_block,
                extra=extra_instructions,
                source_text=source_text
            )
        except KeyError as e:
            raise ValueError(f"Ошибка в шаблоне промпта: отсутствует ключ {e}")