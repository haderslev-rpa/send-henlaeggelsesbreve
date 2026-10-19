"""Entry point til send-henlaeggelsesbreve.

Fyld køen:
    uv run --env-file .env python main.py --queue

Behandl næste item:
    uv run --env-file .env python main.py

Debug:
    uv run --env-file .env python main.py --debug

main.py ejer browser, login, API-klient, recorder og work item-afslutning.
behandel.py udfører forretningsprocessen.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Any, Final

import configuration


# Indstillingen skal sættes, før Playwright, BrowserSession og q-insubiz
# importeres. Den gælder for denne Python-proces og dens underprocesser.
_playwright_skip_browser_gc = getattr(
    configuration,
    "PLAYWRIGHT_SKIP_BROWSER_GC",
    True,
)

if not isinstance(_playwright_skip_browser_gc, bool):
    raise TypeError(
        "configuration.PLAYWRIGHT_SKIP_BROWSER_GC skal være True eller False."
    )

os.environ["PLAYWRIGHT_SKIP_BROWSER_GC"] = (
    "1" if _playwright_skip_browser_gc else "0"
)


# Disse imports kan indlæse Playwright og skal derfor stå efter
# PLAYWRIGHT_SKIP_BROWSER_GC-konfigurationen.
from automation_server_client import (  # noqa: E402
    AutomationServer,
    WorkItemError,
    Workqueue,
)
from playwright.async_api import Page  # noqa: E402
from q_haderslev_vbo.automation_server.ats_update_item_data import (  # noqa: E402
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import (  # noqa: E402
    BrowserSession,
)
from q_insubiz.api.client import InsubizApiClient  # noqa: E402
from q_insubiz.api_client import (  # noqa: E402
    create_api_client_from_context,
)
from q_insubiz.functionality.launch import launch_insubiz  # noqa: E402

from behandel import behandel_page  # noqa: E402
from populate_queue import populate_queue  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)


COMPLETED_STATUS: Final[str] = "Completed"
COMPLETED_STATUS_CODE: Final[str] = "Færdig"
AFSLUTTENDE_BEHANDLINGSSTATE: Final[str] = (
    "4.0 Kommentar om Digital Post oprettet"
)


def _parse_arguments() -> argparse.Namespace:
    """Læser kommandolinjeargumenterne."""
    parser = argparse.ArgumentParser(
        description="Send henlæggelsesbreve via Insubiz og Digital Post."
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Fylder køen.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Udvidet debug-logning.",
    )
    return parser.parse_args()


def _log_playwright_miljoe() -> None:
    """Logger ikke-følsomme Playwright-miljøindstillinger."""
    logger.info(
        "Playwright-miljø: PLAYWRIGHT_SKIP_BROWSER_GC=%s, "
        "PLAYWRIGHT_BROWSERS_PATH=%s.",
        os.environ.get("PLAYWRIGHT_SKIP_BROWSER_GC", "[ikke sat]"),
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "[ikke sat]"),
    )


def _initialiser_automation_server() -> None:
    """Initialiserer forbindelsen til Automation Server."""
    try:
        AutomationServer.from_environment()
    except Exception as error:
        raise RuntimeError(
            "Forbindelsen til Automation Server kunne ikke initialiseres."
        ) from error


def _hent_queue_id() -> int | None:
    """Henter et eventuelt workqueue-id fra configuration.py."""
    get_queue_id = getattr(configuration, "get_queue_id", None)
    queue_id = (
        get_queue_id()
        if callable(get_queue_id)
        else getattr(configuration, "QUEUE_ID", None)
    )

    if queue_id is None:
        return None

    if isinstance(queue_id, bool):
        raise TypeError("QUEUE_ID må ikke være boolsk.")

    try:
        normalized_queue_id = int(str(queue_id).strip())
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            "QUEUE_ID skal være et positivt heltal eller None. "
            f"Modtog: {queue_id!r}."
        ) from error

    if normalized_queue_id <= 0:
        raise RuntimeError(
            "QUEUE_ID skal være større end 0. "
            f"Modtog: {normalized_queue_id}."
        )

    return normalized_queue_id


def _hent_queue_name() -> str:
    """Henter et eventuelt workqueue-navn fra configuration.py."""
    get_queue_name = getattr(configuration, "get_queue_name", None)

    if callable(get_queue_name):
        queue_name = get_queue_name()
    else:
        queue_name = (
            getattr(configuration, "QUEUE_NAME", None)
            or getattr(configuration, "WORKQUEUE_NAME", None)
            or getattr(configuration, "WORK_QUEUE_NAME", None)
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
                "Workqueuen kunne ikke hentes via id. "
                f"Queue-id: {queue_id}."
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
                "Workqueuen kunne ikke hentes via navn. "
                f"Kønavn: {queue_name!r}."
            ) from error

        logger.info(
            "Workqueue hentet via navn. Kønavn: %s.",
            queue_name,
        )
        return workqueue

    raise RuntimeError(
        "Workqueue-konfigurationen mangler. Angiv configuration.QUEUE_ID "
        "eller configuration.QUEUE_NAME."
    )


def _hent_headless(*, debug: bool, queue_mode: bool) -> bool:
    """Returnerer headless-indstillingen for den valgte mode."""
    if queue_mode:
        headless = configuration.QUEUE_HEADLESS
    elif debug:
        headless = configuration.DEBUG_HEADLESS
    else:
        headless = configuration.HEADLESS

    if not isinstance(headless, bool):
        raise TypeError("Browserens headless-indstilling skal være boolsk.")

    return headless


async def _opret_browser_session(
    *,
    debug: bool,
    queue_mode: bool,
) -> tuple[BrowserSession, Page, InsubizApiClient]:
    """Starter én browser, logger ind og opretter klient på samme context."""
    headless = _hent_headless(
        debug=debug,
        queue_mode=queue_mode,
    )

    logger.info(
        "Starter BrowserSession. queue_mode=%s, debug=%s, headless=%s.",
        queue_mode,
        debug,
        headless,
    )

    session = BrowserSession(
        headless=headless,
        debug=debug,
    )
    api_client: InsubizApiClient | None = None

    try:
        await session.start()
        logger.info("BrowserSession.start() blev gennemført.")

        page = await session.new_page()

        if session.context is None:
            raise RuntimeError(
                "BrowserSession oprettede ikke en BrowserContext."
            )

        if page is None:
            raise RuntimeError(
                "BrowserSession oprettede ikke en Playwright-side."
            )

        if page.is_closed():
            raise RuntimeError(
                "BrowserSession oprettede en lukket Playwright-side."
            )

        if page.context is not session.context:
            raise RuntimeError(
                "Playwright-siden tilhører ikke BrowserSessions "
                "BrowserContext."
            )

        if session.recorder is None:
            raise RuntimeError(
                "BrowserSession mangler sin recorder efter start()."
            )

        logger.info("Starter login i Insubiz.")

        await launch_insubiz(
            page=page,
            recorder=session.recorder,
        )

        logger.info("Insubiz-login blev gennemført.")

        api_client = create_api_client_from_context(
            context=session.context,
            page=page,
        )

        if api_client is None:
            raise RuntimeError(
                "Insubiz API-klienten kunne ikke oprettes."
            )

        logger.info(
            "Insubiz-login gennemført. UI og API deler BrowserContext."
        )

        return session, page, api_client

    except BaseException as error:
        logger.exception(
            "BrowserSession eller Insubiz-login kunne ikke startes. "
            "Fejltype: %s.",
            type(error).__name__,
        )
        await _tag_screenshot_ved_fejl(
            session=session,
            error=error,
        )
        await _luk_browser_session(
            browser_session=session,
            api_client=api_client,
        )
        raise


async def _luk_browser_session(
    *,
    browser_session: BrowserSession | None,
    api_client: InsubizApiClient | None = None,
) -> None:
    """Frigiver API-klienten og lukker derefter BrowserSession."""
    try:
        if api_client is not None:
            try:
                await api_client.close()
            except Exception:
                logger.warning(
                    "API-klienten kunne ikke frigives.",
                    exc_info=True,
                )
    finally:
        if browser_session is not None:
            try:
                await browser_session.close()
            except Exception:
                logger.warning(
                    "BrowserSession kunne ikke lukkes.",
                    exc_info=True,
                )


async def _tag_screenshot_ved_fejl(
    *,
    session: BrowserSession | None,
    error: BaseException,
) -> None:
    """Forsøger fejlscreenshot uden at skjule den oprindelige fejl."""
    try:
        if session is None or session.context is None:
            return

        pages = [
            page
            for page in session.context.pages
            if not page.is_closed()
        ]

        if not pages:
            return

        await session.screenshot(
            pages[-1],
            f"exception_{type(error).__name__}",
            always=True,
        )
    except Exception:
        logger.warning(
            "Fejlscreenshot kunne ikke gemmes.",
            exc_info=True,
        )


def _hent_item_reference(item: Any) -> str:
    """Henter itemets reference til logging."""
    return str(getattr(item, "reference", "") or "[ukendt]")


def _hent_naeste_item(workqueue: Workqueue) -> Any | None:
    """Henter næste item fra workqueuen."""
    for method_name in (
        "get_next_item",
        "get_next_work_item",
        "get_item",
    ):
        method = getattr(workqueue, method_name, None)

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


def _kontroller_afsluttende_state(item: Any) -> dict[str, Any]:
    """Forhindrer afslutning før sidste procestrin er gemt."""
    data = getattr(item, "data", None)

    if not isinstance(data, dict):
        raise RuntimeError(
            "Work itemet mangler gyldige data ved afslutning."
        )

    states = data.get("state")

    if not isinstance(states, list):
        raise RuntimeError(
            "Work itemets state-felt skal være en liste; "
            "køstatus ændres ikke."
        )

    har_afsluttende_state = any(
        isinstance(state, str)
        and AFSLUTTENDE_BEHANDLINGSSTATE in state
        for state in states
    )

    if not har_afsluttende_state:
        raise RuntimeError(
            "Work itemet mangler den afsluttende behandlingsstate; "
            "køstatus ændres ikke."
        )

    return data


def _afslut_item(item: Any) -> None:
    """Afslutter ATS-itemet uden at tilføje en Completed-state."""
    from afslut_skade import STATE_SKADE_AFSLUTTET
    from behandel import _har_state

    data = _kontroller_afsluttende_state(item)

    if not _har_state(
        data=data,
        state=STATE_SKADE_AFSLUTTET,
    ):
        raise RuntimeError(
            "State 5.0 mangler; ATS-itemet afsluttes ikke."
        )

    complete_method = getattr(item, "complete", None)

    if not callable(complete_method):
        raise RuntimeError(
            "Work itemet har ingen complete()-metode."
        )

    update_item_data(
        data,
        item=item,
        status=COMPLETED_STATUS,
        status_code=COMPLETED_STATUS_CODE,
    )

    complete_method(COMPLETED_STATUS)


def _fejlmarker_item(*, item: Any, message: str) -> None:
    """Markerer itemet som fejlet."""
    fail_method = getattr(item, "fail", None)

    if not callable(fail_method):
        raise TypeError(
            "Work itemet har ingen fail()-metode. "
            f"Oprindelig fejl: {message}"
        )

    try:
        fail_method(message)
    except TypeError:
        fail_method(error_message=message)


async def _koer_queue_mode(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Fylder køen via browser og delt API-klient uden at rydde køen."""
    logger.info(
        "Queue mode startet. debug=%s.",
        debug,
    )

    session: BrowserSession | None = None
    api_client: InsubizApiClient | None = None

    try:
        session, _page, api_client = await _opret_browser_session(
            debug=debug,
            queue_mode=True,
        )

        await populate_queue(
            workqueue=workqueue,
            debug=debug,
            api_client=api_client,
            recorder=session.recorder,
        )
    except BaseException as error:
        await _tag_screenshot_ved_fejl(
            session=session,
            error=error,
        )
        raise
    finally:
        await _luk_browser_session(
            browser_session=session,
            api_client=api_client,
        )

    logger.info("Queue mode afsluttet.")


async def _koer_process_mode(
    *,
    workqueue: Workqueue,
    debug: bool,
) -> None:
    """Behandler brevet, afslutter skaden og afslutter ATS-itemet."""
    from afslut_skade import (
        STATE_SKADE_AFSLUTTET,
        afslut_skade_efter_brev,
    )

    logger.info(
        "Process mode startet. debug=%s.",
        debug,
    )

    browser_session: BrowserSession | None = None
    api_client: InsubizApiClient | None = None

    try:
        browser_session, page, api_client = await _opret_browser_session(
            debug=debug,
            queue_mode=False,
        )
        recorder = browser_session.recorder

        item = _hent_naeste_item(workqueue)

        if item is None:
            logger.info("Der findes ingen items til behandling.")
            return

        reference = _hent_item_reference(item)

        logger.info(
            "Behandler work item. Reference: %s.",
            reference,
        )

        with item:
            try:
                await behandel_page(
                    item=item,
                    session=browser_session,
                    page=page,
                    api_client=api_client,
                    recorder=recorder,
                )

                await afslut_skade_efter_brev(
                    item=item,
                    api_client=api_client,
                )

                data = item.data
                states = (
                    data.get("state")
                    if isinstance(data, dict)
                    else None
                )

                har_skadeafslutningsstate = (
                    isinstance(states, list)
                    and any(
                        isinstance(state, str)
                        and STATE_SKADE_AFSLUTTET in state
                        for state in states
                    )
                )

                if not har_skadeafslutningsstate:
                    raise WorkItemError(
                        "Afslutningsstate for Insubiz mangler. "
                        "ATS-itemet afsluttes ikke."
                    )

            except WorkItemError as error:
                logger.error(
                    "Work item kunne ikke behandles. "
                    "Reference: %s. Fejl: %s",
                    reference,
                    error,
                )
                await _tag_screenshot_ved_fejl(
                    session=browser_session,
                    error=error,
                )
                _fejlmarker_item(
                    item=item,
                    message=str(error),
                )
                return

            except Exception as error:
                logger.exception(
                    "Work item fejlede med en uventet fejl. "
                    "Reference: %s.",
                    reference,
                )
                await _tag_screenshot_ved_fejl(
                    session=browser_session,
                    error=error,
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
                "Skaden og ATS-itemet blev afsluttet. "
                "Reference: %s.",
                reference,
            )

    except BaseException as error:
        await _tag_screenshot_ved_fejl(
            session=browser_session,
            error=error,
        )
        raise
    finally:
        await _luk_browser_session(
            browser_session=browser_session,
            api_client=api_client,
        )


async def _async_main(*, queue_mode: bool, debug: bool) -> None:
    """Initialiserer og starter den valgte proceskørsel."""
    _log_playwright_miljoe()
    _initialiser_automation_server()

    validate_config = getattr(configuration, "validate_config", None)
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
