"""Security policy binding provider credentials to their network destination."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit


def provider_credential_env(provider_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", provider_id).upper()
    return f"SAIVERSE_PROVIDER_{normalized}_API_KEY"


def model_credential_env(model_key: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]", "_", model_key).upper()
    return f"SAIVERSE_MODEL_{normalized}_API_KEY"


def _explicitly_allowed_hosts() -> set[str]:
    return {
        value.strip().lower()
        for value in os.getenv("SAIVERSE_PROVIDER_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }


def validate_provider_url(base_url: str) -> None:
    """Reject credential destinations that can reach undeclared local services."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provider base_url must not contain credentials, query, or fragment")

    hostname = parsed.hostname.lower()
    allowed = _explicitly_allowed_hosts()
    loopback_names = {"localhost", "localhost.localdomain"}
    if parsed.scheme == "http" and hostname not in allowed and hostname not in loopback_names:
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("Plain HTTP provider URLs require loopback or an explicit allowed host")
        except ValueError as exc:
            if str(exc).startswith("Plain HTTP"):
                raise
            raise ValueError("Plain HTTP provider URLs require loopback or an explicit allowed host") from exc

    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except OSError as exc:
        raise ValueError(f"Provider host could not be resolved: {hostname}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback and (hostname in loopback_names or hostname in allowed or hostname == address):
            continue
        if hostname in allowed:
            continue
        if not ip.is_global:
            raise ValueError(
                f"Provider host resolves to a non-public address ({address}); "
                "add the host to SAIVERSE_PROVIDER_ALLOWED_HOSTS to permit it"
            )


def validate_provider_config(provider_id: str, config: dict) -> None:
    """Bind a provider's credential to the data layer that declared it.

    The split is by who wrote a definition. ``builtin_data/`` ships with
    SAIVerse and ``user_data/`` is written by the person running it — through
    the UI or by hand — so a credential paired with an endpoint in either is a
    choice someone made on purpose, including keeping a shipped key name while
    overriding a shipped provider. ``expansion_data/`` holds add-on packages,
    whose provider JSON nobody here chose, so those may only name this
    provider's own namespaced variable.

    ``source`` comes from the root the loader walked
    (:func:`saiverse.provider_configs.load_configs`), never from the file, so a
    definition cannot declare itself trusted. A config with no ``source`` is
    treated as untrusted, so forgetting to stamp one fails closed.

    **Scope, stated plainly**: this constrains what an add-on can *declare*, not
    what an add-on can *do*. Add-on tools are imported and executed in-process
    (``tools/__init__.py`` calls ``exec_module`` on them), so add-on code can
    already read ``os.environ`` and open its own connections, and can write into
    ``user_data/`` as well. Nothing here sandboxes add-on code; that boundary
    would have to be a separate mechanism.
    """
    from saiverse.provider_configs import SOURCE_BUILTIN, SOURCE_USER_DATA

    base_url = config.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        validate_provider_url(base_url.strip())

    source = config.get("source")
    if source in (SOURCE_BUILTIN, SOURCE_USER_DATA):
        return

    # From here on the definition is untrusted, so it must say plainly which
    # credential it wants. Staying silent is not neutral: clients fall back to
    # a well-known variable when no name is given (llm_clients/openai.py uses
    # OPENAI_API_KEY), which would hand the owner's key to whatever base_url
    # this definition chose. Untrusted definitions therefore either declare
    # they need no key, or name their own namespaced variable.
    api_key_env = config.get("api_key_env")
    expected = provider_credential_env(provider_id)
    origin = f"Provider {provider_id!r} was not configured by the owner (source={source or 'unknown'})"

    # Shape first: an empty or non-string value must not slip through as
    # "nothing was named", which is the very state that triggers the fallback.
    if api_key_env is not None and (
        not isinstance(api_key_env, str) or not api_key_env.strip()
    ):
        raise ValueError(
            f"{origin}; api_key_env must be a non-empty string or omitted entirely, "
            f"got {api_key_env!r} — an empty one reads as 'no name given' and falls "
            f"back to a shipped key"
        )

    if api_key_env is None:
        if config.get("api_key_required") is False:
            return  # declares it needs no credential; nothing will be sent
        raise ValueError(
            f"{origin}; it must either declare api_key_required: false or name "
            f"{expected} — leaving api_key_env unset would fall back to a shipped key"
        )

    if api_key_env.strip() != expected:
        raise ValueError(f"{origin}; it must use {expected} instead of {api_key_env}")

    # The namespaced name is only a safe grant while nobody else reads the same
    # variable. Ids that differ in punctuation collapse to one name
    # ('addon-bar' and 'addon_bar' both give ..._ADDON_BAR_...), and any other
    # provider may simply have been configured with this variable outright. So
    # compare against what the others actually use, not against the names their
    # ids would generate — otherwise a provider quietly reading the owner's
    # variable stays invisible, while an unrelated id merely spelled alike gets
    # refused for nothing.
    from saiverse.provider_configs import PROVIDER_CONFIGS

    # Compared case-insensitively: Windows resolves environment variables
    # without regard to case, so a differently-cased spelling reads the very
    # same value at runtime. On POSIX this only ever refuses more than it must,
    # and refusing an untrusted definition is the safe direction.
    sharers = sorted(
        other
        for other, other_config in PROVIDER_CONFIGS.items()
        if other != provider_id
        and isinstance(other_config.get("api_key_env"), str)
        and other_config["api_key_env"].strip().casefold() == expected.casefold()
    )
    if sharers:
        raise ValueError(
            f"{origin}; {expected} is already read by "
            f"{', '.join(repr(s) for s in sharers)} — rename this provider so its "
            f"credential is its own"
        )


def validate_model_config_connection(model_key: str, config: dict) -> None:
    """Ensure a model cannot pair a known secret with an unrelated endpoint."""
    from saiverse.provider_configs import (
        PROVIDER_CONFIGS,
        SOURCE_BUILTIN,
        SOURCE_USER_DATA,
        get_provider,
    )

    base_url = config.get("base_url")
    api_key_env = config.get("api_key_env")
    provider_ref = config.get("provider_ref")

    if provider_ref:
        provider = get_provider(provider_ref)
        if provider is None:
            raise ValueError(f"Unknown provider_ref: {provider_ref}")
        validate_provider_config(provider_ref, provider)
        # ``is not None``, not truthiness: inheritance from the provider only
        # fills a field the model left absent, so an explicit empty string
        # keeps the provider's endpoint while erasing its credential name —
        # and an erased name is what makes the client fall back to a shipped
        # key. A model may omit the field, or repeat the provider's; nothing else.
        if api_key_env is not None and api_key_env != provider.get("api_key_env"):
            raise ValueError(
                f"Model {model_key!r} sets api_key_env={api_key_env!r}, which does not "
                f"match its provider_ref credential "
                f"({provider.get('api_key_env')!r}); omit it to inherit"
            )
        # ``is not None`` for the same reason as api_key_env above: inheritance
        # only fills an absent field, so an explicit falsy value survives, keeps
        # the provider's credential, and leaves the client with no destination
        # of its own — which means it falls back to the SDK default. A model
        # writing base_url: "" under provider_ref: openrouter would send
        # OPENROUTER_API_KEY to api.openai.com.
        if base_url is not None and (
            not isinstance(base_url, str)
            or not base_url.strip()
            or base_url.strip().rstrip("/")
            != str(provider.get("base_url") or "").rstrip("/")
        ):
            raise ValueError(
                f"Model {model_key!r} sets base_url={base_url!r}, which does not match "
                f"its provider_ref destination ({provider.get('base_url')!r}); "
                f"omit it to inherit"
            )
        # An untrusted provider earns its endpoint by promising no credential is
        # sent (api_key_required: false with no api_key_env). That promise is
        # only worth anything if the model cannot revoke it: with no key name,
        # an OpenAI-compatible client falls back to a shipped variable, so a
        # model flipping api_key_required back to true would ship the owner's
        # key to the add-on's endpoint.
        # Only when the provider named no variable at all: that is the case
        # where its keyless declaration is the sole reason it was allowed, and
        # where revoking it leaves the client with nothing to use but a shipped
        # default. A provider that does name a variable passes it down by
        # inheritance, so a model omitting the field is the normal form and must
        # keep working — the danger is the explicit ``true``, not the silence.
        # Anything explicit that is not a real JSON ``false`` counts as revoking
        # it. Keying on ``is True`` alone would let 1 / "true" / [] through, and
        # downstream only ``is False`` reads as keyless, so those all land in the
        # same fallback the check exists to prevent.
        model_declares = config.get("api_key_required")
        if (
            provider.get("source") not in (SOURCE_BUILTIN, SOURCE_USER_DATA)
            and not provider.get("api_key_env")
            and model_declares is not None
            and model_declares is not False
        ):
            raise ValueError(
                f"Model {model_key!r} cancels the keyless declaration of "
                f"provider {provider_ref!r}, which was the only reason that "
                f"provider may omit a credential name; leave api_key_required "
                f"alone or name {provider_credential_env(provider_ref)}"
            )
        return

    if isinstance(base_url, str) and base_url.strip():
        validate_provider_url(base_url.strip())
    # Present-but-empty is not the same as absent: an empty name still lands in
    # the client's fallback, so it is refused outright. A genuinely absent name
    # on this legacy direct path is still accepted for now — closing that needs
    # the model layer stamped too, see
    # docs/issues/model_layer_not_stamped_credential_fallback.md
    if api_key_env is not None and (
        not isinstance(api_key_env, str) or not api_key_env.strip()
    ):
        raise ValueError(
            f"Model {model_key!r} sets api_key_env={api_key_env!r}; it must be a "
            f"non-empty string or omitted entirely — an empty one falls back to a "
            f"shipped key while keeping this model's own base_url"
        )
    # A model naming its own destination is subject to the same rule as a
    # provider: only a definition the owner wrote may leave the credential
    # unnamed, because an unnamed one is not "no credential" — the client
    # substitutes a shipped variable and sends it wherever this base_url points.
    if (
        config.get("source") not in (SOURCE_BUILTIN, SOURCE_USER_DATA)
        and isinstance(base_url, str)
        and base_url.strip()
        and not api_key_env
        and config.get("api_key_required") is not False
    ):
        raise ValueError(
            f"Model {model_key!r} was not declared by the owner "
            f"(source={config.get('source') or 'unknown'}); pointing at "
            f"{base_url.strip()} it must either declare api_key_required: false "
            f"or name {model_credential_env(model_key)} — leaving api_key_env "
            f"unset would fall back to a shipped key"
        )

    if not api_key_env:
        return

    if api_key_env == model_credential_env(model_key):
        # Same reason as the provider-side check: this name is a safe grant only
        # while nobody else reads that variable. Model keys collapse the same
        # way ('foo-bar' and 'foo_bar' both give ..._FOO_BAR_...), so an
        # untrusted model could otherwise claim a variable the owner set for a
        # different one. Compared case-insensitively for Windows.
        if config.get("source") not in (SOURCE_BUILTIN, SOURCE_USER_DATA):
            from saiverse.model_configs import MODEL_CONFIGS

            wanted = api_key_env.casefold()
            sharers = sorted(
                f"model {other!r}"
                for other, other_config in MODEL_CONFIGS.items()
                if other != model_key
                and isinstance(other_config.get("api_key_env"), str)
                and other_config["api_key_env"].strip().casefold() == wanted
            ) + sorted(
                f"provider {pid!r}"
                for pid, provider in PROVIDER_CONFIGS.items()
                if isinstance(provider.get("api_key_env"), str)
                and provider["api_key_env"].strip().casefold() == wanted
            )
            if sharers:
                raise ValueError(
                    f"Model {model_key!r} was not declared by the owner "
                    f"(source={config.get('source') or 'unknown'}); {api_key_env} is "
                    f"already read by {', '.join(sharers)} — rename this model so its "
                    f"credential is its own"
                )
        return
    for provider_id, provider in PROVIDER_CONFIGS.items():
        # Same rule as validate_provider_config: a pairing counts as declared
        # only if it came from a layer the owner controls.
        if provider.get("source") not in (SOURCE_BUILTIN, SOURCE_USER_DATA):
            continue
        if provider.get("api_key_env") != api_key_env:
            continue
        if str(provider.get("base_url") or "").rstrip("/") == str(base_url or "").rstrip("/"):
            return
    raise ValueError(
        f"Model credential {api_key_env!r} is not bound to this endpoint; "
        f"use provider_ref or {model_credential_env(model_key)}"
    )
