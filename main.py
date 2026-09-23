import asyncio
import logging
import sys
import os
from pprint import pprint  # helper (pæn print)

# ------------------------------------------------------------
# 🧠 PROCESS-KODE (ÉT ITEM)
# ------------------------------------------------------------
from behandel import behandel_page  # funktion (genbrugelig kodeblok)

# ------------------------------------------------------------
# 🧠 AUTOMATION SERVER
# ------------------------------------------------------------
from automation_server_client import (
    AutomationServer,
    Workqueue,
    WorkItemError,
    WorkItemStatus
)

from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data
)


from q_haderslev_vbo.automation_server.ats_is_item_in_queue import (
    is_item_in_queue,
)


def _vaelg_items_til_behandling(workqueue: Workqueue):
    """
    Vælger items til behandling.

    Output:
    - Hvis DEBUG_ITEM_REFERENCE i .env mangler eller er tom:
      Returneres selve workqueue til normal behandling.

    - Hvis DEBUG_ITEM_REFERENCE har en værdi:
      Returneres en liste med kun det første fundne NEW-item.
      Itemet ændres til status "in progress".
    """

    item_reference = os.getenv(
        "DEBUG_ITEM_REFERENCE",
        "",
    ).strip()

    if not item_reference:
        return workqueue

    items = workqueue.get_item_by_reference(
        reference=item_reference,
        status=WorkItemStatus.NEW,
    )

    if not items:
        raise RuntimeError(
            f"Ingen NEW-items fundet med reference: {item_reference}"
        )

    item = items[0]

    item.update_status(
        WorkItemStatus.IN_PROGRESS.value,
        "Startet via lokal debugkørsel",
    )

    return [item]


# ------------------------------------------------------------
# 🌐 PLAYWRIGHT (KAN SLETTES I PROCESSER UDEN BROWSER)
# ------------------------------------------------------------
from q_haderslev_vbo.playwright.browser_session import BrowserSession

def get_headless_flag():  #Skriv HEADLESS=false i .env for at se browseren under kørsel
    return os.getenv("HEADLESS", "true").lower() == "true"


# ------------------------------------------------------------
# LOGGING (STANDARD)
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("automation_server_client").setLevel(logging.WARNING)
logging.getLogger("debugpy").setLevel(logging.WARNING)


# ------------------------------------------------------------
# QUEUE-MODE (PRODUCER)
# ------------------------------------------------------------
async def populate_queue(workqueue: Workqueue, debug: bool):
    logger = logging.getLogger(__name__)
    logger.info("Populate queue mode started")

    # ❗ Ingen Playwright her (standard Automation Server, men kan tilføjes)
    raw_items = [
        {"cpr": "1234567891", "type": "adresseopslag"},
        {"cpr": "1111111111", "type": "fødselsdato"},
        {"cpr": "2222222222", "type": "myndighed"},
    ]

    for raw_item in raw_items:
        data_json = {}

        update_item_data(
            data_json,
            box_updates=raw_item,
            update=False
        )


        item_reference = data_json["box"]["cpr"]

        # Kontrollér om item allerede venter eller behandles.
        if is_item_in_queue(
            queue_id= #INDSÆT ID på QUEUE - men skal gerne laves fra .env eller automation server ved ved ikke hvordan endnu.
            item_reference=item_reference,
            new=True,
            in_progress=True,
            completed=True,
            new=True,
            pending_user_action=True
            start_datetime="2025-07-01T00:00:00Z",
            end_datetime="2026-07-31T23:59:59.999999Z",
            updated_at=False,
        ):
            print(
                f"Springer over: Item med reference "
                f"'{item_reference}' findes allerede i køen."
            )
            continue

        workqueue.add_item(
            data=data_json,
            reference=item_reference,
        )

        print(
            f"Item med reference '{item_reference}' "
            "er tilføjet til køen."
        )




    


# ------------------------------------------------------------
# PROCESS-MODE (WORKER)
# ------------------------------------------------------------
async def process_workqueue(workqueue: Workqueue, debug: bool):
    logger = logging.getLogger(__name__)
    logger.info(f"Process workqueue mode started (debug={debug})")

    # =========================================================
    # 🌐 PLAYWRIGHT – ÉN BROWSERSESSION FOR HELE PROCESSEN
    #
    # ✅ KAN SLETTES i processer uden browser
    # =========================================================
    headless = get_headless_flag()
    session = BrowserSession(headless=headless,debug=debug)
    await session.start()
    page = await session.new_page()  # Page (browser-fane)

    try: # denne try bruges kun til PLAYWRIGHT processer
        # Workqueue er iterable → hvert item behandles ét ad gangen
        for item in _vaelg_items_til_behandling(workqueue): #DEBUG_ITEM_REFERENCE=xxx i .env hvis man vil hente bestemte item.

            with item:
                data = item.data

                try:
                    print("==================================== NEXT ITEM ====================================")
                    print(f"ITEM = ID: {item.id} - Reference: {item.reference}")

                    # --------------------------------------------------
                    # ▶ PROCESS-KODE
                    # (behandel_page bruger Playwright internt)
                    # --------------------------------------------------
                    await behandel_page(item=item, session=session, page=page) #Fjern session og page hvis du ikke bruger Playwright i din process

                    update_item_data(
                        data,
                        item=item,
                        status="Completed",
                        status_code="Færdig",
                        state="Completed",

                    )

                    item.update(data)
                    item.complete("Completed")

                except WorkItemError as e:
                    # =================================================
                    # ✅ SOFT ERROR
                    # - Item fejler
                    # =================================================
                    logger.error(f"WorkItemError for item {item.reference}: {e}")
                    item.fail(str(e))
                    
                    # Playwright:
                    # Luk browser for sikkerhed (ny session på næste item)
                    headless = get_headless_flag()
                    session = BrowserSession(headless=headless,debug=debug)
                    await session.start()

                except Exception as e:
                    # =================================================
                    # ❌ HARD ERROR
                    # - Screenshot tages
                    # - Browser lukkes
                    # - Processen STOPPER
                    # =================================================
                    logger.exception("Uventet fejl")

                    try: #Playwright:
                        if session.context and session.context.pages:
                            page = session.context.pages[-1]
                            await session.screenshot(
                                page,
                                f"hard_exception_{type(e).__name__}",
                                always=True
                            )
                    except Exception:
                        logger.warning("Kunne ikke tage screenshot ved hard error")

                    # Luk ALT (Playwright)
                    await session.close()

                    # Stop hele processen (Automation Server genstarter)
                    raise

    finally: # PLAYWRIGHT:
        # =====================================================
        # 🧹 SIKKER OPRYDNING
        #
        # ✅ Lukker browser hvis processen afsluttes normalt
        # =====================================================
        await session.close() # denne try bruges kun til PLAYWRIGHT processer og kan slettes


# ------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------
if __name__ == "__main__":

    # ✅ CLI flags (runtime-parametre)
    DEBUG = "--debug" in sys.argv   # bool (sand/falsk)
    QUEUE_MODE = "--queue" in sys.argv

    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    # --------------------------------------------------------
    # QUEUE-MODE
    # --------------------------------------------------------
    if QUEUE_MODE:
        # ---------------------------------------------------------------
        # VIGTIGT:
        # Denne linje CLEARSER alle NEW items i køen.
        #
        # ❗ Hvis du ALDRIG vil slette eksisterende NEW items:
        #     → så SKAL denne linje fjernes eller kommenteres ud.
        #
        # workqueue.clear_workqueue(WorkItemStatus.NEW)
        
        workqueue.clear_workqueue(WorkItemStatus.NEW)
        asyncio.run(populate_queue(workqueue, debug=DEBUG))
        sys.exit(0)

    # --------------------------------------------------------
    # PROCESS-MODE
    # --------------------------------------------------------
    asyncio.run(process_workqueue(workqueue, debug=DEBUG))
