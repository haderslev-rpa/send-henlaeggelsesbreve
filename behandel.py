async def behandel_page(item, session, page):

    from q_haderslev_vbo.automation_server.ats_update_item_data import update_item_data
    from q_haderslev_vbo.automation_server.ats_find_state import find_state
    import logging
    logger = logging.getLogger(__name__)

    data = item.data

    """
    PLAYWRIGHT – NY STANDARD (VIGTIGT):

    - BrowserSession (objekt – browser-livscyklus) oprettes i main.py
    - session gives IND i behandel_page som parameter
    - behandel_page må:
        ✅ bruge page.goto() (funktion – navigér)
        ✅ tage screenshots via await session.recorder.screenshot(page=page,"cura_efter_login",always=True)
        ✅ tage optage video via await session.recorder.start_recording(page=page, 10)
    - behandel_page må IKKE:
        ❌ oprette BrowserSession
        ❌ lukke session eller browser 
        ❌ bruge session.new_page() (funktion – ny fane) (kan godt være undtagelser)
        ❌ have try/except/finally for Playwright
        ❌Exception-håndtering, screenshots ved fejl og session.close() åndteres ALTID i main.py.
    
    SHAREPOINT LIST INTEGRATION
    from q_haderslev_vbo.automation_server.ats_sharepoint import (
    hent_sharepoint_list_item_til_box,
    gem_sharepoint_list_item_til_box
    )

    # HENT
    hent_sharepoint_list_item_til_box(
        site_name="Automatisering",
        list_name="Test - Rune",
        list_item_id=item.data["box"]["sharepoint"]["id"],
        item=item
    )

    # GEM (UPDATE)
    gem_sharepoint_list_item_til_box(
        site_name="Automatisering",
        list_name="Test - Rune",
        sharepoint_data={
            "id": item.data["box"]["sharepoint"]["id"],
            "Robot kommentar": "Sag afsluttet"
        },
        item=item
)
    behandel_page(item, session, page):
    
    Eksempel på hvordan sharepoint kan bruges
  1. Læs data = item.data
  2. Sørg for at box findes
  3. (Evt.) hent SharePoint ind i box.sharepoint
  4. Brug box + box.sharepoint til logik
  5. Når noget ændres:
       - opdatér box (lokalt)
       - gem i SharePoint (via gem_..._til_box)
  6. Opdatér states/status
  7. Lad ATS gemme item.data
    """

    # ==========================================================
    # 🧠 STATES
    # ==========================================================
    class States:
        SEND_BREV = "1.0 Brev sendt"
        JOURNALISER_BREV = "1.5 Brev journaliseret"
        AFSLUT_SAG = "2.0 Sag afsluttet"


    # ==========================================================
    # 🔁 HELPERS
    # ==========================================================
    def mangler_state(state, step):
        states = data.get("state", [])

        match = next((s for s in states if state in s), None)

        if match:
            log_step(step, f'Skip "{match}"')
            return False

        return True

    def set_state(state):
        update_item_data(data, item=item, state=state)

    def log_step(step, text):
        logger.info(f"[{step}] {text}")


    # ==========================================================
    step = "SEND_BREV"
    # ==========================================================
    state = getattr(States, step)

    if mangler_state(state, step):

        log_step(step, "Start")

        data["box"]["brev_sendt_id"] = 123
        log_step(step, f'ID sat: {data["box"]["brev_sendt_id"]}')

        update_item_data(data, item=item)

        set_state(state)


    # ==========================================================
    step = "JOURNALISER_BREV"
    # ==========================================================
    state = getattr(States, step)

    if mangler_state(state, step):

        log_step(step, "Start")

        data["box"]["journal_id"] = 456
        log_step(step, f'ID sat: {data["box"]["journal_id"]}')

        update_item_data(data, item=item)

        set_state(state)


    # ==========================================================
    step = "AFSLUT_SAG"
    # ==========================================================
    state = getattr(States, step)

    if mangler_state(state, step):

        log_step(step, "Start")

        data["box"]["afslutnings_id"] = 789
        log_step(step, f'ID sat: {data["box"]["afslutnings_id"]}')

        update_item_data(data, item=item)

        set_state(state)
