"""Language-role policy shared by Discord adapters; no network calls."""
ROLE_KEYS = {'ru': 'lang_ru', 'en': 'lang_en'}
ROLE_NAMES = {'ru': ('ru', 'language: russian'), 'en': ('engl', 'en', 'language: english')}


def role_ids(config, roles, language):
    stored = config.get('role.' + ROLE_KEYS[language])
    if stored and stored.isdigit():
        return {int(stored)}
    return {role.id for role in roles if str(role.name).casefold() in ROLE_NAMES[language]}


def member_language(config, member, fallback='en'):
    roles = getattr(member, 'roles', [])
    held = {role.id for role in roles}
    selected = [lang for lang in ('ru', 'en') if held & role_ids(config, roles, lang)]
    return selected[0] if len(selected) == 1 else fallback
