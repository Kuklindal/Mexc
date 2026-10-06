"""Фразы для авторежима. Для каждого сообщения выбирается одна фраза из списка."""

import os

PHRASES = {
    "forward_message": ["Здравствуйте! Город Иркутск.", "Привет, Иркутск.", "Я в Красноярске нахожусь", "Привет, Владивосток", "Здорова, Владиосток", "Прив, Владивосток", "Хай, я из Владивостока", "Приветствую нахожусь во Владивостоке", "Здорова, живу в Иркутске"],  # П2 → П1, первая сделка
    "forward_reply": ["Здравствуйте,  менеджер бежит к вам.", "Привет, хорошо. Менеджер идет.", "Направил, к вам менеджера", "Отправил человека к вам", "Отправил к вам человека", "Менеджер спускается", "Отлично, менеджер спускатеся", "Пару минут, менеджер идет", "Пару секунд менеджер, идет"],      # П1 → П2, первая сделка
    "reverse_message": ["Здравствуйте", "Привет", "Приветики", "Хай", "Хей", "Приветствую", "Здорова", "Прив"], # П2 → П1, обратная сделка
    "reverse_reply": ["Отправил","Послал", "Ушли", "Отослал", "Выслал"],      # П1 → П2, обратная сделка
}


# Neutral bank-card wording for Eflp; cash phrases above remain unchanged.
EFLP_PHRASES = {
    "forward_message": ["Здравствуйте, оформил покупку USDT. Оплачу по реквизитам ордера."],
    "forward_reply": ["Здравствуйте, ожидаю оплату по реквизитам ордера."],
    "reverse_message": ["Здравствуйте, продаю USDT по этому ордеру. Ожидаю оплату."],
    "reverse_reply": ["Здравствуйте, оплачу по реквизитам ордера."],
}


def phrases_for_mode(mode: str, env=None) -> dict[str, list[str]]:
    """Resolve four mode-specific chat lists from a single bot's environment."""
    if mode not in {'eflp_volume', 'eflp_unique'}:
        return PHRASES
    source = os.environ if env is None else env
    result = {}
    for key, defaults in EFLP_PHRASES.items():
        field = f'EFLP_{key.upper()}_PHRASES'
        raw = (source.get(field) or '').strip()
        choices = [value.strip() for value in raw.split('|')] if raw else list(defaults)
        if any(not value or len(value) > 2000 or '\n' in value or '\r' in value
               for value in choices):
            raise ValueError(f'{field}: укажите непустые фразы до 2000 символов через |')
        result[key] = choices
    return result
