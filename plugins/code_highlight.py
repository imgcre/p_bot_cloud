from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound


PYGMENTS_LANGUAGE_ALIASES = {
    'assembly': 'nasm',
    'x86asm': 'nasm',
    'x86': 'nasm',
    'x86_64': 'nasm',
    'x64': 'nasm',
}


def normalize_pygments_language(lang: str):
    normalized = lang.strip().lower()
    return PYGMENTS_LANGUAGE_ALIASES.get(normalized, normalized)


def get_code_lexer(content: str, lang: str = ''):
    normalized_lang = normalize_pygments_language(lang)
    try:
        return get_lexer_by_name(normalized_lang) if normalized_lang else guess_lexer(content)
    except ClassNotFound:
        return TextLexer()
