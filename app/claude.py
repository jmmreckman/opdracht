"""Dunne laag rond de Anthropic API: één plek voor model, thinking, caching,
fallbacks en het bijhouden van kosten."""
import logging

import anthropic

from . import config

logger = logging.getLogger("opdracht")

# Prijzen in USD per miljoen tokens (invoer, uitvoer). Alleen voor de
# kosteninschatting op de site; de echte factuur staat in de Anthropic Console.
PRIJZEN = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}
WEB_SEARCH_PRIJS = 10.0 / 1000  # per zoekopdracht

WEB_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 12,
     "user_location": {"type": "approximate", "country": "NL", "timezone": "Europe/Amsterdam"}},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 12},
]

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4, timeout=900)
    return _client


def vraag(system: str, messages: list, tools: list | None = None, max_tokens: int = 16000):
    """Eén API-aanroep. Bij een weigering door de veiligheidsfilters schakelt
    de API zelf over op een ander model (fallbacks='default')."""
    return client().beta.messages.create(
        model=config.claude_model(),
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        tools=tools or [],
        thinking={"type": "adaptive"},
        output_config={"effort": config.claude_effort()},
        cache_control={"type": "ephemeral"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


def inhoud_voor_geschiedenis(content: list) -> list:
    """Wat van een antwoord terug mag in de volgende aanroep. Na een
    fallback midden in een antwoord mogen de denk- en toolblokken van vóór het
    omschakelpunt niet mee terug; tekst wel."""
    laatste = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if laatste < 0:
        return list(content)
    voor = [b for b in content[:laatste] if b.type == "text"]
    return voor + list(content[laatste + 1:])


def kosten(response) -> dict:
    u = response.usage
    invoer = (u.input_tokens or 0)
    cache_schrijf = getattr(u, "cache_creation_input_tokens", 0) or 0
    cache_lees = getattr(u, "cache_read_input_tokens", 0) or 0
    uitvoer = u.output_tokens or 0
    zoek = 0
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        zoek = getattr(stu, "web_search_requests", 0) or 0
    p_in, p_uit = PRIJZEN.get(getattr(response, "model", "") or config.claude_model(),
                              PRIJZEN.get(config.claude_model(), (5.0, 25.0)))
    usd = (invoer * p_in + cache_schrijf * p_in * 1.25 + cache_lees * p_in * 0.1
           + uitvoer * p_uit) / 1_000_000 + zoek * WEB_SEARCH_PRIJS
    return {
        "tokens_in": invoer + cache_schrijf + cache_lees,
        "tokens_uit": uitvoer,
        "zoekopdrachten": zoek,
        "usd": usd,
    }


def tekst_van(content) -> str:
    return "\n".join(b.text for b in content if b.type == "text").strip()
