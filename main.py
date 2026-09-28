from __future__ import annotations

"""Entry point til send-henlaeggelsesbreve.

Kørselsformer
-------------

Fyld køen:
    uv run --env-file .env python main.py --queue

Behandl næste item:
    uv run --env-file .env python main.py

Behandl næste item med screenshots ved Playwright-fejl:
    uv run --env-file .env python main.py --debug

Ansvarsfordeling
----------------

main.py ejer:
- BrowserSession
- Playwright Page
- PlaywrightRunRecorder
- login i Insubiz på procesbrowserens side
- Insubiz API-klient omkring samme BrowserContexts request-session
- lukning af browseren

behandel.py modtager den autentificerede side og API-klient og udfører:
- opslag af skade
- dokumentoprettelse
- oprettelse af EASY-rapport
- Digital Post
"""

import argparse
import asyncio
import inspect
import logging
from typing import Any, Final

from automation_server_client import (
    AutomationServer,
    WorkItemError,
    Workqueue,
)
from playwright.async_api import Page
from q_haderslev_vbo.playwright.browser_session import (
    BrowserSession,
)
from q_haderslev_vbo.playwright.playwright_run_recorder import (
    PlaywrightRunRecorder,
)
from q_insubiz.api_client import (
    create_api_client_from_request_context,
)
from q_insubiz.functionality.launch import (
    launch_insubiz,
)

import config
from behandel import behandel_page
from populate_queue import populate_queue

# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format=("%(asctime)s [%(levelname)s] %(name)s: %(message)s"),
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# STATUSVÆRDIER
# ------------------------------------------------------------

COMPLETED_STATUS: Final[str] = "Completed"
COMPLETED_STATUS_CODE: Final[str] = "Færdig"
COMPLETED_STATE: Final[str] = "Completed"


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------


def _parse_arguments() -> argparse.Namespace:
    """Læser kommandolinjeargumenterne."""
    parser = argparse.ArgumentParser(
        description=("Send henlæggelsesbreve via Insubiz og Digital Post.")
    )

    parser.add_argument(
        "--queue",
        action="store_true",
        help=("Fylder køen i stedet for at behandle et item."),
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help=("Aktiverer Playwright-recorder og screenshots ved UI-fejl."),
    )

    return parser.parse_args()


# ------------------------------------------------------------
# AUTOMATION SERVER
# ------------------------------------------------------------


def _initialiser_automation_server() -> None:
    """Initialiserer forbindelsen til Automation Server."""
    try:
        AutomationServer.from_environment()
    except Exception as error:
        raise RuntimeError(
            "Forbindelsen til Automation Server kunne ikke initialiseres."
        ) from error


def _hent_queue_id() -> int | None:
    """Henter et eventuelt workqueue-id fra config.py."""
    get_queue_id = getattr(
        config,
        "get_queue_id",
        None,
    )

    if callable(get_queue_id):
        queue_id = get_queue_id()
    else:
        queue_id = getattr(
            config,
            "QUEUE_ID",
            None,
        )

    if queue_id is None:
        return None

    if isinstance(queue_id, bool):
        raise TypeError("QUEUE_ID må ikke være boolsk.")

    try:
        normalized_queue_id = int(str(queue_id).strip())
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"QUEUE_ID skal være et positivt heltal eller None. Modtog: {queue_id!r}."
        ) from error

    if normalized_queue_id <= 0:
        raise RuntimeError(
            f"QUEUE_ID skal være større end 0. Modtog: {normalized_queue_id}."
        )

    return normalized_queue_id


def _hent_queue_name() -> str:
    """Henter et eventuelt workqueue-navn fra config.py."""
    get_queue_name = getattr(
        config,
        "get_queue_name",
        None,
    )

    if callable(get_queue_name):
        queue_name = get_queue_name()
    else:
        queue_name = (
            getattr(
                config,
                "QUEUE_NAME",
                None,
            )
            or getattr(
                config,
                "WORKQUEUE_NAME",
                None,
            )
            or getattr(
                config,
                "WORK_QUEUE_NAME",
                None,
            )
            or ""
        )

    return str(queue_name).strip()


def _hent_workqueue() -> Workqueue:
    """Henter workqueuen via id eller navn."""
    queue_id = _hent_queue_id()

    if queue_id is not None:
        try:
            workqueue = Workqueue.get_workqueue(queue_id)
        except Exception as error:
            raise RuntimeError(
                f"Workqueuen kunne ikke hentes via id. Queue-id: {queue_id}."
            ) from error

        logger.info(
            "Workqueue hentet via id. Queue-id: %s.",
            queue_id,
        )

        return workqueue

    queue_name = _hent_queue_name()

    if queue_name:
        try:
            workqueue = Workqueue.get_workqueue_by_name(queue_name)
        except Exception as error:
            raise RuntimeError(
                f"Workqueuen kunne ikke hentes via navn. Kønavn: {queue_name!r}."
            ) from error

        logger.info(
            "Workqueue hentet via navn. Kønavn: %s.",
            queue_name,
        )

        return workqueue

    raise RuntimeError(
        "Workqueue-konfigurationen mangler. "
        "Angiv enten config.QUEUE_ID som et "
        "positivt heltal eller config.QUEUE_NAME "
        "som køens navn."
    )


# ------------------------------------------------------------
# ASYNC-HJÆLPER
# ------------------------------------------------------------


async def _await_if_needed(
    value: Any,
) -> Any:
    """Awaiter værdien, hvis værdien er awaitable."""
    if inspect.isawaitable(value):
        return await value

    return value


# ------------------------------------------------------------
# BROWSERSESSION
# ------------------------------------------------------------


def _hent_headless() -> bool:
    """Henter browserens headless-indstilling."""
    get_headless = getattr(
        config,
        "get_headless",
        None,
    )

    if callable(get_headless):
        headless = get_headless()
    else:
        headless = getattr(
            config,
            "HEADLESS",
            True,
        )

    if not isinstance(headless, bool):
        raise TypeError(f"HEADLESS skal være True eller False. Modtog: {headless!r}.")

    return headless


async def _opret_browser_session() -> BrowserSession:
    """Opretter og starter processens BrowserSession."""
    headless = _hent_headless()

    try:
        browser_session = BrowserSession(
            headless=headless,
        )
    except TypeError:
        browser_session = BrowserSession()

    start_method = getattr(
        browser_session,
        "start",
        None,
    )

    if callable(start_method):
        await _await_if_needed(start_method())

    return browser_session


async def _hent_page(
    *,
    browser_session: BrowserSession,
) -> Page:
    """Henter eller opretter en åben Page."""
    for attribute_name in (
        "page",
        "active_page",
    ):
        page = getattr(
            browser_session,
            attribute_name,
            None,
        )

        if page is None:
            continue

        page = await _await_if_needed(page)

        if page is not None and not page.is_closed():
            return page

    new_page_method = getattr(
        browser_session,
        "new_page",
        None,
    )

    if callable(new_page_method):
        page = await _await_if_needed(new_page_method())

        if page is not None and not page.is_closed():
            return page

    context = getattr(
        browser_session,
        "context",
        None,
    )

    context = await _await_if_needed(context)

    if context is not None:
        page = await context.new_page()

        if page is not None and not page.is_closed():
            return page

    raise RuntimeError(
        "Der kunne ikke hentes eller oprettes en Playwright-side fra BrowserSession."
    )


async def _luk_browser_session(
    *,
    browser_session: BrowserSession | None,
) -> None:
    """Lukker procesbrowseren kontrolleret."""
    if browser_session is None:
        return

    close_method = getattr(
        browser_session,
        "close",
        None,
    )

    if not callable(close_method):
        return

    try:
        await _await_if_needed(close_method())
    except Exception:
        logger.warning(
            "BrowserSession kunne ikke lukkes korrekt.",
            exc_info=True,
        )


# ------------------------------------------------------------
# RECORDER
# ------------------------------------------------------------


def _opret_recorder(
    *,
    browser_session: BrowserSession,
    debug: bool,
) -> PlaywrightRunRecorder | None:
    """Opretter recorderen fra BrowserSession."""
    if not debug:
        return None

    return PlaywrightRunRecorder(
        browser_session=browser_session,
        debug=True,
        always=False,
    )


# ------------------------------------------------------------
# WORK ITEM-HJÆLPERE
# ------------------------------------------------------------


def _hent_item_reference(
    item: Any,
) -> str:
    """Henter itemets reference til logging."""
    return str(
        getattr(
            item,
            "reference",
            "",
        )
        or "[ukendt]"
    )


def _hent_naeste_item(
    workqueue: Workqueue,
) -> Any | None:
    """Henter næste item fra workqueuen."""
    for method_name in (
        "get_next_item",
        "get_next_work_item",
        "get_item",
    ):
        method = getattr(
            workqueue,
            method_name,
            None,
        )

        if not callable(method):
            continue

        try:
            return method()
        except TypeError:
            continue

    try:
        return next(iter(workqueue))
    except StopIteration:
        return None


def _afslut_item(
    item: Any,
) -> None:
    """Marker itemet som færdigt."""
    complete_method = getattr(
        item,
        "complete",
        None,
    )

    if callable(complete_method):
        try:
            complete_method(
                status=COMPLETED_STATUS,
                status_code=COMPLETED_STATUS_CODE,
                state=COMPLETED_STATE,
            )
            return
        except TypeError:
            complete_method()
            return

    raise RuntimeError("Work itemet har ingen understøttet complete()-metode.")


def _fejlmarker_item(
    *,
    item: Any,
    message: str,
) -> None:
    """Marker itemet som fejlet."""
    fail_method = getattr(
        item,
        "fail",
        None,
    )

    if not callable(fail_method):
        raise TypeError(
            f"Work itemet har ingen fail()-metode. Oprindelig fejl: {message}"
        )

    try:
        fail_method(message)
    except TypeError:
        fail_method(error_message=message)


# ------------------------------------------------------------
# QUEUE-MODE
# ------------------------------------------------------------


async def _koer_queue_mode(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Fylder workqueuen med relevante skader."""
    logger.info(
        "Queue mode startet. debug=%s.",
        debug,
    )

    await populate_queue(
        workqueue=workqueue,
        debug=debug,
    )

    logger.info("Queue mode afsluttet.")


# ------------------------------------------------------------
# PROCESS-MODE
# ------------------------------------------------------------


async def _koer_process_mode(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Logger procesbrowseren ind og behandler næste item."""
    logger.info(
        "Process mode startet. debug=%s.",
        debug,
    )

    browser_session: BrowserSession | None = None

    try:
        browser_session = await _opret_browser_session()

        page = await _hent_page(
            browser_session=browser_session,
        )

        recorder = _opret_recorder(
            browser_session=browser_session,
            debug=debug,
        )

        # Login udføres én gang på den samme side,
        # som behandel.py bruger.
        await launch_insubiz(
            page=page,
            recorder=recorder,
        )

        api_client = create_api_client_from_request_context(
            request_context=page.context.request,
        )

        item = _hent_naeste_item(workqueue)

        if item is None:
            logger.info("Der findes ingen items til behandling.")
            return

        reference = _hent_item_reference(item)

        logger.info(
            "Behandler work item. Reference: %s.",
            reference,
        )

        try:
            await behandel_page(
                item=item,
                session=browser_session,
                page=page,
                api_client=api_client,
            )
        except WorkItemError as error:
            logger.error(
                "Work item kunne ikke behandles. Reference: %s. Fejl: %s",
                reference,
                error,
            )

            _fejlmarker_item(
                item=item,
                message=str(error),
            )

            return

        except Exception as error:
            logger.exception(
                "Work item fejlede med en uventet fejl. Reference: %s.",
                reference,
            )

            _fejlmarker_item(
                item=item,
                message=(
                    "Uventet fejl under behandling. "
                    f"Fejl: {type(error).__name__}: {error}"
                ),
            )

            return

        _afslut_item(item)

        logger.info(
            "Work item blev afsluttet. Reference: %s.",
            reference,
        )

    finally:
        if api_client is not None:
            try:
                await api_client.close()
            except Exception:
                logger.warning(
                    "Insubiz API-klienten kunne ikke frigives korrekt.",
                    exc_info=True,
                )

        await _luk_browser_session(
            browser_session=browser_session,
        )


# ------------------------------------------------------------
# ENTRYPOINTS
# ------------------------------------------------------------


async def _async_main(
    *,
    queue_mode: bool,
    debug: bool,
) -> None:
    """Starter den valgte proceskørsel."""
    _initialiser_automation_server()

    validate_config = getattr(
        config,
        "validate_config",
        None,
    )

    if callable(validate_config):
        validate_config()

    workqueue = _hent_workqueue()

    logger.info(
        "Starter Insubiz-processen. queue_mode=%s, debug=%s.",
        queue_mode,
        debug,
    )

    if queue_mode:
        await _koer_queue_mode(
            workqueue=workqueue,
            debug=debug,
        )
        return

    await _koer_process_mode(
        workqueue=workqueue,
        debug=debug,
    )


def main() -> int:
    """Starter processen og returnerer procesexitkoden."""
    arguments = _parse_arguments()

    try:
        asyncio.run(
            _async_main(
                queue_mode=arguments.queue,
                debug=arguments.debug,
            )
        )
        return 0

    except KeyboardInterrupt:
        logger.warning("Processen blev afbrudt manuelt.")
        return 130

    except Exception:
        logger.exception("Processen stoppede med en uventet fejl.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
