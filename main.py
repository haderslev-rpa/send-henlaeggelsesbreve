"""Entry point til send-henlaeggelsesbreve.

Fyld køen: uv run --env-file .env python main.py --queue
Behandl næste item: uv run --env-file .env python main.py
Debug: uv run --env-file .env python main.py --debug

main.py ejer browser, login, API-klient, recorder og work item-afslutning.
behandel.py udfører forretningsprocessen.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any, Final

from automation_server_client import (
    AutomationServer,
    WorkItemError,
    Workqueue,
)
from playwright.async_api import Page
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import (
    BrowserSession,
)
from q_insubiz.api.client import InsubizApiClient
from q_insubiz.api_client import (
    create_api_client_from_context,
)
from q_insubiz.functionality.launch import launch_insubiz

import configuration
from behandel import behandel_page
from populate_queue import populate_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

COMPLETED_STATUS: Final[str] = "Completed"
COMPLETED_STATUS_CODE: Final[str] = "Færdig"
COMPLETED_STATE: Final[str] = "Completed"
AFSLUTTENDE_BEHANDLINGSSTATE: Final[str] = "4.0 Kommentar om Digital Post oprettet"


def _parse_arguments() -> argparse.Namespace:
    """Læser kommandolinjeargumenterne."""
    parser = argparse.ArgumentParser(
        description="Send henlæggelsesbreve via Insubiz og Digital Post."
    )
    parser.add_argument("--queue", action="store_true", help="Fylder køen.")
    parser.add_argument("--debug", action="store_true", help="Udvidet debug-logning.")
    return parser.parse_args()


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
    queue_id = get_queue_id() if callable(get_queue_id) else getattr(configuration, "QUEUE_ID", None)
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
                f"Workqueuen kunne ikke hentes via id. Queue-id: {queue_id}."
            ) from error
        logger.info("Workqueue hentet via id. Queue-id: %s.", queue_id)
        return workqueue
    queue_name = _hent_queue_name()
    if queue_name:
        try:
            workqueue = Workqueue.get_workqueue_by_name(queue_name)
        except Exception as error:
            raise RuntimeError(
                f"Workqueuen kunne ikke hentes via navn. Kønavn: {queue_name!r}."
            ) from error
        logger.info("Workqueue hentet via navn. Kønavn: %s.", queue_name)
        return workqueue
    raise RuntimeError(
        "Workqueue-konfigurationen mangler. Angiv configuration.QUEUE_ID "
        "eller configuration.QUEUE_NAME."
    )





def _hent_headless(*, debug: bool, queue_mode: bool) -> bool:
    """Returnerer headless for den valgte mode uden skjulte fallback-værdier."""
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
    *, debug: bool, queue_mode: bool,
) -> tuple[BrowserSession, Page, InsubizApiClient]:
    """Starter én browser, logger ind og opretter klient på samme context."""
    session = BrowserSession(
        headless=_hent_headless(debug=debug, queue_mode=queue_mode),
        debug=debug,
    )
    api_client: InsubizApiClient | None = None
    try:
        await session.start()
        page = await session.new_page()
        if session.context is None or page.is_closed():
            raise RuntimeError("BrowserSession oprettede ikke en aktiv side og context.")
        if page.context is not session.context:
            raise RuntimeError("Siden tilhører ikke BrowserSessions context.")
        if session.recorder is None:
            raise RuntimeError("BrowserSession mangler sin recorder efter start().")
        await launch_insubiz(page=page, recorder=session.recorder)
        api_client = create_api_client_from_context(
            context=session.context, page=page,
        )
        logger.info("Insubiz-login gennemført. UI og API deler BrowserContext.")
        return session, page, api_client
    except BaseException as error:
        await _tag_screenshot_ved_fejl(session=session, error=error)
        await _luk_browser_session(browser_session=session, api_client=api_client)
        raise





async def _luk_browser_session(
    *, browser_session: BrowserSession | None,
    api_client: InsubizApiClient | None = None,
) -> None:
    """Frigiver klienten og lukker derefter BrowserSession, også ved fejl."""
    try:
        if api_client is not None:
            try:
                await api_client.close()
            except Exception:
                logger.warning("API-klienten kunne ikke frigives.", exc_info=True)
    finally:
        if browser_session is not None:
            try:
                await browser_session.close()
            except Exception:
                logger.warning("BrowserSession kunne ikke lukkes.", exc_info=True)


async def _tag_screenshot_ved_fejl(
    *, session: BrowserSession | None, error: BaseException,
) -> None:
    """Forsøger fejlscreenshot uden at skjule den oprindelige fejl."""
    try:
        if session is None or session.context is None:
            return
        pages = [page for page in session.context.pages if not page.is_closed()]
        if pages:
            await session.screenshot(
                pages[-1], f"exception_{type(error).__name__}", always=True,
            )
    except Exception:
        logger.warning("Fejlscreenshot kunne ikke gemmes.", exc_info=True)





def _hent_item_reference(item: Any) -> str:
    """Henter itemets reference til logging."""
    return str(getattr(item, "reference", "") or "[ukendt]")


def _hent_naeste_item(workqueue: Workqueue) -> Any | None:
    """Henter næste item fra workqueuen."""
    for method_name in ("get_next_item", "get_next_work_item", "get_item"):
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
    """Forhindrer afslutning før det sidste procestrin er gemt."""
    data = getattr(item, "data", None)
    if not isinstance(data, dict):
        raise RuntimeError("Work itemet mangler gyldige data ved afslutning.")
    states = data.get("state")
    if not isinstance(states, list) or not any(
        isinstance(state, str) and AFSLUTTENDE_BEHANDLINGSSTATE in state
        for state in states
    ):
        raise RuntimeError(
            "Work itemet mangler den afsluttende behandlingsstate; "
            "køstatus ændres ikke."
        )
    return data


def _afslut_item(item: Any) -> None:
    """Gem JSON-status og afslut derefter kø-itemet via klientens complete()."""
    data = _kontroller_afsluttende_state(item)
    complete_method = getattr(item, "complete", None)
    if not callable(complete_method):
        raise RuntimeError("Work itemet har ingen complete()-metode.")
    status_data = data.get("status")
    already_recorded = (
        isinstance(status_data, dict)
        and status_data.get("status") == COMPLETED_STATUS
        and status_data.get("status_code") == COMPLETED_STATUS_CODE
    )
    if not already_recorded:
        update_item_data(
            data,
            item=item,
            status=COMPLETED_STATUS,
            status_code=COMPLETED_STATUS_CODE,
            state=COMPLETED_STATE,
        )
    # Et vellykket item.update() ændrer ikke nødvendigvis køens egen status.
    # Undlad at sluge TypeError: en fejl i klienten skal kunne ses i loggen.
    complete_method(COMPLETED_STATUS)


def _fejlmarker_item(*, item: Any, message: str) -> None:
    """Marker itemet som fejlet."""
    fail_method = getattr(item, "fail", None)
    if not callable(fail_method):
        raise TypeError(
            f"Work itemet har ingen fail()-metode. Oprindelig fejl: {message}"
        )
    try:
        fail_method(message)
    except TypeError:
        fail_method(error_message=message)


async def _koer_queue_mode(*, workqueue: Workqueue, debug: bool) -> None:
    """Fylder køen via main.py's browser og delte API-klient. Rydder ikke køen."""
    logger.info("Queue mode startet. debug=%s.", debug)
    session, _page, api_client = await _opret_browser_session(
        debug=debug, queue_mode=True,
    )
    try:
        await populate_queue(
            workqueue=workqueue, debug=debug,
            api_client=api_client, recorder=session.recorder,
        )
    except BaseException as error:
        await _tag_screenshot_ved_fejl(session=session, error=error)
        raise
    finally:
        await _luk_browser_session(browser_session=session, api_client=api_client)
    logger.info("Queue mode afsluttet.")


async def _koer_process_mode(*, workqueue: Workqueue, debug: bool) -> None:
    """Logger ind og behandler næste item inden for itemets kontekst."""
    logger.info("Process mode startet. debug=%s.", debug)
    browser_session: BrowserSession | None = None
    api_client: Any | None = None
    try:
        browser_session, page, api_client = await _opret_browser_session(
            debug=debug, queue_mode=False,
        )
        recorder = browser_session.recorder
        item = _hent_naeste_item(workqueue)
        if item is None:
            logger.info("Der findes ingen items til behandling.")
            return
        reference = _hent_item_reference(item)
        logger.info("Behandler work item. Reference: %s.", reference)
        # Klientens kontekst frigiver låsen ved udgang; complete sker indenfor.
        with item:
            try:
                await behandel_page(
                    item=item,
                    session=browser_session,
                    page=page,
                    api_client=api_client,
                    recorder=recorder,
                )
            except WorkItemError as error:
                logger.error(
                    "Work item kunne ikke behandles. Reference: %s. Fejl: %s",
                    reference,
                    error,
                )
                await _tag_screenshot_ved_fejl(session=browser_session, error=error)
                _fejlmarker_item(item=item, message=str(error))
                return
            except Exception as error:
                logger.exception(
                    "Work item fejlede med en uventet fejl. Reference: %s.",
                    reference,
                )
                await _tag_screenshot_ved_fejl(session=browser_session, error=error)
                _fejlmarker_item(
                    item=item,
                    message=(
                        "Uventet fejl under behandling. "
                        f"Fejl: {type(error).__name__}: {error}"
                    ),
                )
                return
            # En fejl i afslutning skal fremgå tydeligt og må ikke skjules.
            _afslut_item(item)
            logger.info("Work item blev afsluttet. Reference: %s.", reference)
    except BaseException as error:
        await _tag_screenshot_ved_fejl(session=browser_session, error=error)
        raise
    finally:
        await _luk_browser_session(
            browser_session=browser_session, api_client=api_client,
        )


async def _async_main(*, queue_mode: bool, debug: bool) -> None:
    """Starter den valgte proceskørsel."""
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
        await _koer_queue_mode(workqueue=workqueue, debug=debug)
        return
    await _koer_process_mode(workqueue=workqueue, debug=debug)


def main() -> int:
    """Starter processen og returnerer procesexitkoden."""
    arguments = _parse_arguments()
    try:
        asyncio.run(_async_main(queue_mode=arguments.queue, debug=arguments.debug))
        return 0
    except KeyboardInterrupt:
        logger.warning("Processen blev afbrudt manuelt.")
        return 130
    except Exception:
        logger.exception("Processen stoppede med en uventet fejl.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
