"""Token counting, injectable so M2 can swap in the model's official tokenizer"""
from functools import lru_cache

DEFAULT_ENCODING = "o200k_base"


@lru_cache(maxsize=4)
def _encoding(name):
    import tiktoken

    return tiktoken.get_encoding(name)


def count_tokens(text, encoding=DEFAULT_ENCODING):
    return len(_encoding(encoding).encode(text, disallowed_special=()))


def truncate_tokens(text, limit, encoding=DEFAULT_ENCODING):
    enc = _encoding(encoding)
    ids = enc.encode(text, disallowed_special=())
    return text if len(ids) <= limit else enc.decode(ids[:limit])
