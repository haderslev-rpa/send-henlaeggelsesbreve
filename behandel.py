"""Behandler ét work item i Send henlæggelsesbreve-processen.

Forløb
------

1. Hent skade-id og skadenummer fra work itemets box.
2. Hent skaden fra Insubiz via API.
3. Genkontrollér status og standardCase.
4. Find CPR-nummeret i skadedata.
5. Åbn den konkrete skade i Insubiz.
6. Opret henlæggelsesbrevet fra den konfigurerede skabelon.
7. Opret EASY-rapporten og gem den i skadens mappe.
8. Send dokumenterne som Digital Post.
9. Registrér states efter afsluttede handlinger.
10. Returnér til main.py, som afslutter work itemet.

BrowserSession, Page, InsubizApiClient og PlaywrightRunRecorder oprettes og ejes af main.py.

Den samme BrowserContext bruges til:

- login i Insubiz via Playwright
- UI-handlinger i Insubiz
- API-kald via BrowserContextens request-session

behandel.py opretter eller lukker ikke browser, BrowserContext,
APIRequestContext eller API-klient.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from automation_server_client import WorkItemError
from playwright.async_api import (
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.browser_session import (
    BrowserSession,
)
from q_haderslev_vbo.playwright.playwright_run_recorder import (
    PlaywrightRunRecorder,
)
from q_insubiz.api.client import InsubizApiClient
from q_insubiz.functionality.skader import (
    download_easy_rapport_og_gem_i_mappe,
    gem_dokument_fra_skabelon,
    hent_skade_via_id,
    hent_skadens_dokumenter,
    opret_dokument_fra_skabelon,
    opret_kommentar_paa_skade,
    send_digital_post,
    vaelg_dokumentskabelon,
)

import configuration

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# STATES
# ------------------------------------------------------------

STATE_DOKUMENT_OPRETTET = "1.0 Oprettet dokument fra skabelon"

STATE_EASY_RAPPORT_OPRETTET = "2.0 Oprettet EASY-rapport"

STATE_DIGITAL_POST_SENDT = "3.0 Sendt digital post"

STATE_KOMMENTAR_OPRETTET = "4.0 Kommentar om Digital Post oprettet"

AFSLUTTENDE_STATES = (STATE_KOMMENTAR_OPRETTET,)


# ------------------------------------------------------------
# WORK ITEM-FELTER
# ------------------------------------------------------------

SKADE_ID_FELTER = (
    "Skade_id",
    "skade_id",
    "Skade id",
    "Skade-id",
    "IncidentId",
    "incident_id",
)

SKADE_NR_FELTER = (
    "Skade_nr",
    "skade_nr",
    "Skade nr",
    "Skade nr.",
    "Skade-nr",
    "Skadenr",
    "Skadenr.",
    "IncidentNumberInternal",
)


# ------------------------------------------------------------
# BOX-FELTER TIL DIGITAL POST OG KOMMENTAR
# ------------------------------------------------------------

BOX_DIGITAL_POST_HOVEDDOKUMENT = "Digital Post hoveddokument"
BOX_DIGITAL_POST_BILAG = "Digital Post bilag"
BOX_DIGITAL_POST_KOMMENTAR_ID = "Digital Post kommentar-id"
BOX_DIGITAL_POST_HOVEDDOKUMENT_ID = "Digital Post hoveddokument-id"
BOX_DIGITAL_POST_HOVEDDOKUMENT_OPRETTET = (
    "Digital Post hoveddokument-oprettet"
)
BOX_DIGITAL_POST_BILAG_ID = "Digital Post bilag-id"
BOX_DIGITAL_POST_BILAG_OPRETTET = "Digital Post bilag-oprettet"
BOX_DIGITAL_POST_DOKUMENTER_OPRETTET_AF = (
    "Digital Post dokumenter oprettet af"
)


# ------------------------------------------------------------
# API-FELTER
# ------------------------------------------------------------

STATUS_FELTER = (
    "IncidentStatus",
    "incidentStatus",
    "Incident Status",
    "Status",
    "status",
)

STANDARD_CASE_FELTER = (
    "standardCase",
    "StandardCase",
    "standard case",
    "Standard case",
)


# ------------------------------------------------------------
# PUBLIC BEHANDLINGSFUNKTION
# ------------------------------------------------------------


async def behandel_page(
    *,
    item: Any,
    session: BrowserSession,
    page: Page,
    api_client: InsubizApiClient,
    recorder: PlaywrightRunRecorder | None = None,
) -> None:
    """Behandler ét henlæggelsesbrev-work item."""
    _valider_browserobjekter(
        session=session,
        page=page,
    )

    _valider_api_client(
        api_client=api_client,
    )

    data = getattr(
        item,
        "data",
        None,
    )

    if not isinstance(data, dict):
        raise WorkItemError(
            f"Work item data skal være en dictionary. Modtog: {type(data).__name__}."
        )

    box = _hent_box(
        data=data,
    )

    skade_id = _hent_skade_id_fra_box(
        box=box,
    )

    skade_nr = _hent_skade_nr_fra_box(
        box=box,
    )

    if _har_afsluttende_state(
        data=data,
    ):
        logger.info(
            "Work itemet har allerede en afsluttende state. "
            "Skade-id: %s. "
            "Skade-nr.: %s.",
            skade_id,
            skade_nr,
        )

        return

    print()
    print("=" * 80)
    print("SEND HENLÆGGELSESBREV")
    print("=" * 80)
    print(f"Skade-id: {skade_id}")
    print(f"Skade-nr.: {skade_nr}")

    try:
        # --------------------------------------------------
        # KONFIGURATION
        # --------------------------------------------------

        skabelon_navn = _hent_konfigurationstekst(
            attribute_name="SKABELON_NAVN",
            field_name="SKABELON_NAVN",
        )

        dokumenttitel = _hent_konfigurationstekst(
            attribute_name="DOKUMENTTITEL",
            field_name="DOKUMENTTITEL",
        )

        forsendelsestype = _hent_konfigurationstekst(
            attribute_name="FORSENDELSESTYPE",
            field_name="FORSENDELSESTYPE",
        )

        hoveddokument_navn = _hent_konfigurationstekst(
            attribute_name="HOVEDDOKUMENT_NAVN",
            field_name="HOVEDDOKUMENT_NAVN",
        )

        bilag_navn = _hent_konfigurationstekst(
            attribute_name="BILAG_NAVN",
            field_name="BILAG_NAVN",
        )

        # --------------------------------------------------
        # HENT SKADEN VIA DEN DELTE API-KLIENT
        # --------------------------------------------------

        skade = await hent_skade_via_id(
            api_client=api_client,
            skade_id=skade_id,
        )

        _kontroller_skade_id(
            skade=skade,
            forventet_skade_id=skade_id,
        )

        # --------------------------------------------------
        # GENKONTROLLÉR AKTUEL STATUS
        # --------------------------------------------------

        aktuel_status = _hent_aktuel_status(
            skade=skade,
        )

        statusser_der_ikke_maa_behandles = _hent_statusser_der_ikke_maa_behandles()

        if _normaliser_tekst(aktuel_status) in statusser_der_ikke_maa_behandles:
            raise WorkItemError(
                "Skaden må ikke behandles på grund af dens "
                "aktuelle status. "
                f"Status: {aktuel_status}. "
                f"Skade-id: {skade_id}. "
                f"Skade-nr.: {skade_nr}."
            )

        # --------------------------------------------------
        # GENKONTROLLÉR STANDARD CASE
        # --------------------------------------------------

        aktuel_standard_case = _hent_standard_case(
            skade=skade,
        )

        box_standard_case = _hent_standard_case_fra_box(
            box=box,
        )

        if box_standard_case is not aktuel_standard_case:
            _opdater_standard_case_i_box(
                data=data,
                item=item,
                standard_case=aktuel_standard_case,
            )

        if not aktuel_standard_case:
            raise WorkItemError(
                "Skaden er ikke længere standardCase "
                "og kræver manuel behandling. "
                f"Skade-id: {skade_id}. "
                f"Skade-nr.: {skade_nr}."
            )

        # --------------------------------------------------
        # HENT CPR-NUMMER
        # --------------------------------------------------

        cpr_nummer = _hent_cpr_nummer(
            skade=skade,
        )

        logger.info(
            "CPR-nummer blev fundet i skadedata. "
            "Skade-id: %s. "
            "CPR-nummerets længde: %s.",
            skade_id,
            len(cpr_nummer),
        )

        # --------------------------------------------------
        # ÅBN DEN KONKRETE SKADE
        # --------------------------------------------------

        await _aabn_skade_via_id(
            page=page,
            skade_id=skade_id,
        )

        # --------------------------------------------------
        # OPRET HENLÆGGELSESBREV
        # --------------------------------------------------

        if not _har_state(
            data=data,
            state=STATE_DOKUMENT_OPRETTET,
        ):
            dialog = await opret_dokument_fra_skabelon(
                page=page,
                recorder=recorder,
            )

            valgt_skabelon = await vaelg_dokumentskabelon(
                page=page,
                dialog=dialog,
                skabelon_navn=skabelon_navn,
                recorder=recorder,
            )

            await gem_dokument_fra_skabelon(
                page=page,
                dialog=dialog,
                recorder=recorder,
            )

            _registrer_state(
                data=data,
                item=item,
                state=STATE_DOKUMENT_OPRETTET,
            )

            logger.info(
                "Henlæggelsesbrev blev oprettet. Skade-id: %s. Skabelon: %r.",
                skade_id,
                valgt_skabelon,
            )

            print(f"Dokument oprettet: {valgt_skabelon}")

        else:
            logger.info(
                "Dokumentstate findes allerede. "
                "Dokumentet oprettes ikke igen. "
                "Skade-id: %s.",
                skade_id,
            )

            print("Dokument allerede oprettet i en tidligere kørsel.")

        # --------------------------------------------------
        # OPRET EASY-RAPPORT
        # --------------------------------------------------

        if not _har_state(
            data=data,
            state=STATE_EASY_RAPPORT_OPRETTET,
        ):
            await download_easy_rapport_og_gem_i_mappe(
                page=page,
                recorder=recorder,
            )

            _registrer_state(
                data=data,
                item=item,
                state=STATE_EASY_RAPPORT_OPRETTET,
            )

            logger.info(
                "EASY-rapport blev oprettet og gemt på skaden. Skade-id: %s.",
                skade_id,
            )

            print("EASY-rapport oprettet og gemt på skaden.")

        else:
            logger.info(
                "EASY-rapportstate findes allerede. "
                "Rapporten oprettes ikke igen. "
                "Skade-id: %s.",
                skade_id,
            )

            print("EASY-rapport allerede oprettet i en tidligere kørsel.")

        # --------------------------------------------------
        # HENT OG FASTLÅS DOKUMENTER TIL DIGITAL POST
        # --------------------------------------------------
        valgte_dokumenter: dict[str, Any] | None = None

        if not _har_state(
            data=data,
            state=STATE_DIGITAL_POST_SENDT,
        ):
            dokumentrespons = await hent_skadens_dokumenter(
                api_client=api_client,
                skade_id=skade_id,
                directory_id=configuration.DOKUMENT_DIRECTORY_ID,
            )

            valgte_dokumenter = _vaelg_dokumenter_til_digital_post(
                dokumentrespons=dokumentrespons,
                skade_id=skade_id,
                skade_nr=skade_nr,
                hoveddokument_navn=hoveddokument_navn,
                bilag_navn=bilag_navn,
            )

            hoveddokument_navn = valgte_dokumenter[
                "hoveddokument_titel"
            ]
            bilag_navn = valgte_dokumenter["bilag_titel"]

            update_item_data(
                data,
                item=item,
                box_updates={
                    BOX_DIGITAL_POST_HOVEDDOKUMENT_ID: (
                        valgte_dokumenter["hoveddokument_id"]
                    ),
                    BOX_DIGITAL_POST_HOVEDDOKUMENT_OPRETTET: (
                        valgte_dokumenter[
                            "hoveddokument_oprettet"
                        ]
                    ),
                    BOX_DIGITAL_POST_BILAG_ID: (
                        valgte_dokumenter["bilag_id"]
                    ),
                    BOX_DIGITAL_POST_BILAG_OPRETTET: (
                        valgte_dokumenter["bilag_oprettet"]
                    ),
                    BOX_DIGITAL_POST_DOKUMENTER_OPRETTET_AF: (
                        valgte_dokumenter["oprettet_af"]
                    ),
                },
            )

            logger.info(
                "Dokumenter til Digital Post blev valgt via "
                "Insubiz-metadata. Skade-id: %s. "
                "Hoveddokument-id: %s. Bilag-id: %s. "
                "Oprettet af: %r. Hoveddokument oprettet: %s. "
                "Bilag oprettet: %s.",
                skade_id,
                valgte_dokumenter["hoveddokument_id"],
                valgte_dokumenter["bilag_id"],
                valgte_dokumenter["oprettet_af"],
                valgte_dokumenter["hoveddokument_oprettet"],
                valgte_dokumenter["bilag_oprettet"],
            )

        # --------------------------------------------------
        # SEND DIGITAL POST
        # --------------------------------------------------

        if not _har_state(
            data=data,
            state=STATE_DIGITAL_POST_SENDT,
        ):
            digital_post_test = _hent_digital_post_test()

            resultat = await send_digital_post(
                page=page,
                cpr_nummer=cpr_nummer,
                dokumenttitel=dokumenttitel,
                forsendelsestype=forsendelsestype,
                hoveddokument_navn=hoveddokument_navn,
                bilag_navn=bilag_navn,
                hoveddokument_id=(
                    valgte_dokumenter[
                        "hoveddokument_id"
                    ]
                ),
                bilag_id=(
                    valgte_dokumenter[
                        "bilag_id"
                    ]
                ),
                hoveddokument_oprettet_af=(
                    valgte_dokumenter[
                        "hoveddokument_oprettet_af"
                    ]
                ),
                bilag_oprettet_af=(
                    valgte_dokumenter[
                        "bilag_oprettet_af"
                    ]
                ),
                hoveddokument_oprettet=(
                    valgte_dokumenter[
                        "hoveddokument_oprettet"
                    ]
                ),
                bilag_oprettet=(
                    valgte_dokumenter[
                        "bilag_oprettet"
                    ]
                ),
                test=digital_post_test,
                recorder=recorder,
            )

            _valider_digital_post_resultat(
                resultat=resultat,
                test=digital_post_test,
                skade_id=skade_id,
                dokumenttitel=dokumenttitel,
                forsendelsestype=forsendelsestype,
                hoveddokument_navn=hoveddokument_navn,
                bilag_navn=bilag_navn,
            )

            if digital_post_test:
                raise WorkItemError(
                    "Digital Post blev kun udfyldt i "
                    "testtilstand og blev ikke sendt. "
                    "Der registreres derfor ikke en "
                    "afsluttende sendestate. "
                    f"Skade-id: {skade_id}."
                )

            faktisk_hoveddokument = _normaliser_paakraevet_tekst(
                value=resultat.get("faktisk_hoveddokument"),
                field_name="faktisk_hoveddokument",
            )
            faktisk_bilag = _normaliser_paakraevet_tekst(
                value=resultat.get("faktisk_bilag"),
                field_name="faktisk_bilag",
            )

            update_item_data(
                data,
                item=item,
                box_updates={
                    BOX_DIGITAL_POST_HOVEDDOKUMENT: faktisk_hoveddokument,
                    BOX_DIGITAL_POST_BILAG: faktisk_bilag,
                },
            )

            _registrer_state(
                data=data,
                item=item,
                state=STATE_DIGITAL_POST_SENDT,
            )

            logger.info(
                "Digital Post blev sendt. "
                "Skade-id: %s. "
                "Skade-nr.: %s. "
                "Hoveddokument: %r. "
                "Bilag: %r.",
                skade_id,
                skade_nr,
                faktisk_hoveddokument,
                faktisk_bilag,
            )

            print("Digital Post blev sendt.")
            print(f"Hoveddokument: {faktisk_hoveddokument}")
            print(f"Bilag: {faktisk_bilag}")

        else:
            logger.info(
                "Digital Post-state findes allerede. "
                "Digital Post sendes ikke igen. "
                "Skade-id: %s.",
                skade_id,
            )

            print("Digital Post allerede sendt i en tidligere kørsel.")

        # --------------------------------------------------
        # OPRET KOMMENTAR PÅ SKADEN
        # --------------------------------------------------

        if not _har_state(
            data=data,
            state=STATE_KOMMENTAR_OPRETTET,
        ):
            box = _hent_box(data=data)

            faktisk_hoveddokument = _normaliser_paakraevet_tekst(
                value=box.get(BOX_DIGITAL_POST_HOVEDDOKUMENT),
                field_name=f"box.{BOX_DIGITAL_POST_HOVEDDOKUMENT}",
            )
            faktisk_bilag = _normaliser_paakraevet_tekst(
                value=box.get(BOX_DIGITAL_POST_BILAG),
                field_name=f"box.{BOX_DIGITAL_POST_BILAG}",
            )

            tidspunkt = datetime.now(configuration.KOMMENTAR_TIDSZONE)
            titel = (
                configuration.KOMMENTAR_TITEL_PREFIX
                + tidspunkt.strftime(configuration.KOMMENTAR_DATOFORMAT)
            )
            kommentartekst = (
                f"{configuration.KOMMENTAR_HOVEDDOKUMENT_LABEL}: "
                f"{faktisk_hoveddokument}\n"
                f"{configuration.KOMMENTAR_BILAG_LABEL}: {faktisk_bilag}"
            )

            kommentar = await opret_kommentar_paa_skade(
                request_context=page.context.request,
                skade_id=skade_id,
                titel=titel,
                kommentartekst=kommentartekst,
            )

            if not isinstance(kommentar, dict):
                raise WorkItemError(
                    "opret_kommentar_paa_skade returnerede ikke en dictionary. "
                    f"Skade-id: {skade_id}. "
                    f"Modtog: {type(kommentar).__name__}."
                )

            kommentar_id = kommentar.get("id")
            if (
                isinstance(kommentar_id, bool)
                or not isinstance(kommentar_id, int)
                or kommentar_id <= 0
            ):
                raise WorkItemError(
                    "Kommentaroprettelsen returnerede ikke et gyldigt "
                    "kommentar-id. "
                    f"Skade-id: {skade_id}. "
                    "Kontrollér Insubiz før genkørsel."
                )

            update_item_data(
                data,
                item=item,
                box_updates={
                    BOX_DIGITAL_POST_KOMMENTAR_ID: kommentar_id,
                },
            )

            _registrer_state(
                data=data,
                item=item,
                state=STATE_KOMMENTAR_OPRETTET,
            )

            logger.info(
                "Kommentar om Digital Post blev oprettet. "
                "Skade-id: %s. Skade-nr.: %s. Kommentar-id: %s. "
                "Titel: %r. Hoveddokument: %r. Bilag: %r.",
                skade_id,
                skade_nr,
                kommentar_id,
                titel,
                faktisk_hoveddokument,
                faktisk_bilag,
            )

            print("Kommentar blev oprettet på skaden.")
            print(f"Kommentar-id: {kommentar_id}")
            print(f"Titel: {titel}")
            print(f"Hoveddokument: {faktisk_hoveddokument}")
            print(f"Bilag: {faktisk_bilag}")

        else:
            logger.info(
                "Kommentarstate findes allerede. "
                "Kommentaren oprettes ikke igen. "
                "Skade-id: %s.",
                skade_id,
            )

            print("Kommentar allerede oprettet i en tidligere kørsel.")

        print("=" * 80)
        print("RESULTAT: Behandlingen er gennemført.")
        print("Itemet kan afsluttes som Completed.")
        print("=" * 80)
        print()

    except WorkItemError:
        raise

    except Exception as error:
        logger.exception(
            "Behandlingen af henlæggelsesbrevet fejlede. Skade-id: %s. Skade-nr.: %s.",
            skade_id,
            skade_nr,
        )

        raise WorkItemError(
            "Behandlingen af henlæggelsesbrevet fejlede. "
            f"Skade-id: {skade_id}. "
            f"Skade-nr.: {skade_nr}. "
            f"Fejl: {type(error).__name__}: {error}"
        ) from error


# ------------------------------------------------------------
# INSUBIZ-NAVIGATION
# ------------------------------------------------------------


async def _aabn_skade_via_id(
    *,
    page: Page,
    skade_id: int,
) -> None:
    """Åbner og bekræfter den konkrete skade i Insubiz."""
    if page.is_closed():
        raise WorkItemError(
            "Skaden kunne ikke åbnes, fordi Playwright-siden er lukket."
        )

    normalized_skade_id = _normaliser_positivt_heltal(
        value=skade_id,
        field_name="skade_id",
    )

    skade_url = f"{configuration.INSUBIZ_BASE_URL}/incident/{normalized_skade_id}"

    logger.info(
        "Åbner skade i Insubiz. Skade-id: %s.",
        normalized_skade_id,
    )

    try:
        response = await page.goto(
            skade_url,
            wait_until="domcontentloaded",
            timeout=configuration.NAVIGATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise WorkItemError(
            "Navigation til skaden fik timeout. "
            f"Skade-id: {normalized_skade_id}. "
            f"URL: {skade_url}. "
            "Timeout: "
            f"{configuration.NAVIGATION_TIMEOUT_MS // 1_000} sekunder."
        ) from error

    if response is not None and response.status >= 400:
        raise WorkItemError(
            "Navigation til skaden returnerede "
            "en HTTP-fejl. "
            f"Skade-id: {normalized_skade_id}. "
            f"HTTP-status: {response.status}. "
            f"URL: {page.url}."
        )

    await page.wait_for_load_state("domcontentloaded")

    await page.wait_for_timeout(configuration.UI_WAIT_MS)

    forventet_url_del = f"/incident/{normalized_skade_id}"

    if forventet_url_del in page.url:
        logger.info(
            "Skaden blev åbnet i Insubiz. Skade-id: %s. URL: %s.",
            normalized_skade_id,
            page.url,
        )

        return

    synligt_skade_id = page.get_by_text(
        str(normalized_skade_id),
        exact=True,
    ).first

    try:
        await synligt_skade_id.wait_for(
            state="visible",
            timeout=configuration.NAVIGATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as error:
        raise WorkItemError(
            "Den ønskede skade kunne ikke bekræftes "
            "som aktiv i Insubiz. "
            f"Skade-id: {normalized_skade_id}. "
            f"Aktuel URL: {page.url}."
        ) from error

    logger.info(
        "Skaden blev bekræftet via synligt skade-id. Skade-id: %s. URL: %s.",
        normalized_skade_id,
        page.url,
    )


# ------------------------------------------------------------
# DOKUMENTVALG VIA INSUBIZ API
# ------------------------------------------------------------
def _vaelg_dokumenter_til_digital_post(
    *,
    dokumentrespons: list[dict[str, Any]] | dict[str, Any],
    skade_id: int,
    skade_nr: str,
    hoveddokument_navn: str,
    bilag_navn: str,
) -> dict[str, Any]:
    """Vælger det nyeste sammenhørende dokumentpar til Digital Post.

    Begge dokumenter skal være PDF-filer og være oprettet af den bruger,
    der er angivet i configuration.DOKUMENT_OPRETTET_AF. Dokumenterne
    skal samtidig være oprettet inden for den konfigurerede tidsforskel.

    Output:
        Returnerer et dictionary med entydige dokument-id'er, titler,
        filnavne, oprettelsestidspunkter og oprettende bruger.
    """
    dokumenter = _udpak_dokumentliste(
        dokumentrespons=dokumentrespons,
        skade_id=skade_id,
    )

    forventet_bruger = _hent_konfigurationstekst(
        attribute_name="DOKUMENT_OPRETTET_AF",
        field_name="DOKUMENT_OPRETTET_AF",
    )

    directory_id = getattr(
        configuration,
        "DOKUMENT_DIRECTORY_ID",
        None,
    )
    if (
        isinstance(directory_id, bool)
        or not isinstance(directory_id, int)
        or directory_id < 0
    ):
        raise WorkItemError(
            "configuration.DOKUMENT_DIRECTORY_ID skal være "
            "et ikke-negativt heltal."
        )

    tidsforskel_sekunder = getattr(
        configuration,
        "DOKUMENT_TIDSFORSKEL_SEKUNDER",
        None,
    )
    if (
        isinstance(tidsforskel_sekunder, bool)
        or not isinstance(tidsforskel_sekunder, int)
        or tidsforskel_sekunder < 0
    ):
        raise WorkItemError(
            "configuration.DOKUMENT_TIDSFORSKEL_SEKUNDER skal "
            "være et ikke-negativt heltal."
        )

    forventet_hoveddokument_titel = _normaliser_paakraevet_tekst(
        value=hoveddokument_navn,
        field_name="hoveddokument_navn",
    )
    forventet_bilagstitel = (
        f"{_normaliser_paakraevet_tekst(value=bilag_navn, field_name='bilag_navn')}"
        f" - {skade_nr}"
    )

    hoveddokumenter: list[dict[str, Any]] = []
    bilag: list[dict[str, Any]] = []

    for dokument in dokumenter:
        if not _dokument_er_oprettet_af(
            dokument=dokument,
            forventet_bruger=forventet_bruger,
        ):
            continue

        if not _dokument_er_pdf(dokument=dokument):
            continue

        titel = _hent_dokumenttekst(
            dokument=dokument,
            feltnavn="title",
        )

        if _tekster_er_ens(
            titel,
            forventet_hoveddokument_titel,
        ):
            hoveddokumenter.append(dokument)
            continue

        if _tekster_er_ens(
            titel,
            forventet_bilagstitel,
        ):
            bilag.append(dokument)

    if not hoveddokumenter:
        raise WorkItemError(
            "Dokumentfanen indeholder ikke et hoveddokument med den "
            "forventede titel og oprettende bruger. "
            f"Forventet titel: {forventet_hoveddokument_titel!r}. "
            f"Forventet bruger: {forventet_bruger!r}. "
            f"Skade-id: {skade_id}."
        )

    if not bilag:
        raise WorkItemError(
            "Dokumentfanen indeholder ikke et EASY-bilag med den "
            "forventede titel og oprettende bruger. "
            f"Forventet titel: {forventet_bilagstitel!r}. "
            f"Forventet bruger: {forventet_bruger!r}. "
            f"Skade-id: {skade_id}."
        )

    dokumentpar: list[
        tuple[
            datetime,
            int,
            int,
            dict[str, Any],
            dict[str, Any],
        ]
    ] = []
    maksimal_tidsforskel = timedelta(
        seconds=tidsforskel_sekunder,
    )

    for hoveddokument in hoveddokumenter:
        hoveddokument_oprettet = _hent_dokumenttidspunkt(
            dokument=hoveddokument,
        )

        for aktuelt_bilag in bilag:
            bilag_oprettet = _hent_dokumenttidspunkt(
                dokument=aktuelt_bilag,
            )
            tidsforskel = abs(
                hoveddokument_oprettet - bilag_oprettet
            )

            if tidsforskel > maksimal_tidsforskel:
                continue

            dokumentpar.append(
                (
                    max(
                        hoveddokument_oprettet,
                        bilag_oprettet,
                    ),
                    _hent_dokument_id(
                        dokument=hoveddokument,
                    ),
                    _hent_dokument_id(
                        dokument=aktuelt_bilag,
                    ),
                    hoveddokument,
                    aktuelt_bilag,
                )
            )

    if not dokumentpar:
        raise WorkItemError(
            "Der blev fundet relevante dokumenter, men ikke et "
            "sammenhørende dokumentpar med næsten samme "
            "oprettelsestidspunkt. "
            f"Maksimal tidsforskel: {tidsforskel_sekunder} sekunder. "
            f"Antal hoveddokumenter: {len(hoveddokumenter)}. "
            f"Antal bilag: {len(bilag)}. "
            f"Skade-id: {skade_id}."
        )

    dokumentpar.sort(
        key=lambda value: (
            value[0],
            value[1],
            value[2],
        ),
        reverse=True,
    )

    (
        _,
        hoveddokument_id,
        bilag_id,
        hoveddokument,
        valgt_bilag,
    ) = dokumentpar[0]

    hoveddokument_oprettet = _hent_dokumenttidspunkt(
        dokument=hoveddokument,
    )
    bilag_oprettet = _hent_dokumenttidspunkt(
        dokument=valgt_bilag,
    )

    return {
        "hoveddokument_id": hoveddokument_id,
        "hoveddokument_titel": _hent_dokumenttekst(
            dokument=hoveddokument,
            feltnavn="title",
        ),
        "hoveddokument_filnavn": _hent_dokumenttekst(
            dokument=hoveddokument,
            feltnavn="filename",
        ),
        "hoveddokument_oprettet": (
            hoveddokument_oprettet.isoformat()
        ),
        "bilag_id": bilag_id,
        "bilag_titel": _hent_dokumenttekst(
            dokument=valgt_bilag,
            feltnavn="title",
        ),
        "bilag_filnavn": _hent_dokumenttekst(
            dokument=valgt_bilag,
            feltnavn="filename",
        ),
        "bilag_oprettet": bilag_oprettet.isoformat(),
        "oprettet_af": forventet_bruger,
    }


def _udpak_dokumentliste(
    *,
    dokumentrespons: list[dict[str, Any]] | dict[str, Any],
    skade_id: int,
) -> list[dict[str, Any]]:
    """Finder dokumentposter rekursivt i getDirectory-svaret."""
    dokumenter: list[dict[str, Any]] = []
    sete_dokument_ider: set[int] = set()

    def gennemgaa(value: Any) -> None:
        if isinstance(value, dict):
            if (
                "title" in value
                and "id" in value
                and (
                    "filename" in value
                    or "fileType" in value
                )
            ):
                try:
                    dokument_id = _hent_dokument_id(
                        dokument=value,
                    )
                except WorkItemError:
                    dokument_id = 0

                if (
                    dokument_id > 0
                    and dokument_id not in sete_dokument_ider
                ):
                    sete_dokument_ider.add(dokument_id)
                    dokumenter.append(value)

            for child in value.values():
                gennemgaa(child)

        elif isinstance(value, list):
            for child in value:
                gennemgaa(child)

    gennemgaa(dokumentrespons)

    if not dokumenter:
        raise WorkItemError(
            "Dokumentresponsen indeholder ingen læsbare dokumentposter. "
            f"Skade-id: {skade_id}."
        )

    return dokumenter


def _dokument_er_oprettet_af(
    *,
    dokument: dict[str, Any],
    forventet_bruger: str,
) -> bool:
    """Kontrollerer createdBy.name på et dokument."""
    created_by = dokument.get("createdBy")

    if not isinstance(created_by, dict):
        return False

    return _tekster_er_ens(
        created_by.get("name"),
        forventet_bruger,
    )


def _dokument_er_pdf(
    *,
    dokument: dict[str, Any],
) -> bool:
    """Kontrollerer dokumentets filtype."""
    file_type = dokument.get("fileType")

    if (
        isinstance(file_type, str)
        and file_type.strip().casefold() == "pdf"
    ):
        return True

    filename = dokument.get("filename")
    return (
        isinstance(filename, str)
        and filename.strip().casefold().endswith(".pdf")
    )


def _hent_dokument_id(
    *,
    dokument: dict[str, Any],
) -> int:
    """Henter dokumentets positive Insubiz-id."""
    value = dokument.get("id")

    if isinstance(value, bool):
        raise WorkItemError(
            "Dokumentets id må ikke være boolsk."
        )

    try:
        dokument_id = int(value)
    except (TypeError, ValueError) as error:
        raise WorkItemError(
            "Dokumentet mangler et gyldigt id. "
            f"Titel: {dokument.get('title')!r}."
        ) from error

    if dokument_id <= 0:
        raise WorkItemError(
            "Dokumentets id skal være større end 0. "
            f"Titel: {dokument.get('title')!r}. "
            f"Dokument-id: {dokument_id}."
        )

    return dokument_id


def _hent_dokumenttekst(
    *,
    dokument: dict[str, Any],
    feltnavn: str,
) -> str:
    """Henter et obligatorisk tekstfelt fra dokumentmetadata."""
    return _normaliser_paakraevet_tekst(
        value=dokument.get(feltnavn),
        field_name=f"dokument.{feltnavn}",
    )


def _hent_dokumenttidspunkt(
    *,
    dokument: dict[str, Any],
) -> datetime:
    """Henter created med fileCreated som fallback."""
    value = (
        dokument.get("created")
        or dokument.get("fileCreated")
    )

    if not isinstance(value, str) or not value.strip():
        raise WorkItemError(
            "Dokumentet mangler created og fileCreated. "
            f"Dokument-id: {dokument.get('id')!r}. "
            f"Titel: {dokument.get('title')!r}."
        )

    normaliseret = value.strip()

    if normaliseret.endswith(("Z", "z")):
        normaliseret = normaliseret[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(normaliseret)
    except ValueError as error:
        raise WorkItemError(
            "Dokumentets oprettelsestidspunkt har et ugyldigt format. "
            f"Dokument-id: {dokument.get('id')!r}. "
            f"Værdi: {value!r}."
        ) from error


# ------------------------------------------------------------
# DIGITAL POST-RESULTAT
# ------------------------------------------------------------


def _valider_digital_post_resultat(
    *,
    resultat: Any,
    test: bool,
    skade_id: int,
    dokumenttitel: str,
    forsendelsestype: str,
    hoveddokument_navn: str,
    bilag_navn: str,
) -> None:
    """Validerer resultatet fra send_digital_post()."""
    if not isinstance(resultat, dict):
        raise WorkItemError(
            "send_digital_post returnerede et "
            "ugyldigt resultat. "
            f"Skade-id: {skade_id}. "
            f"Modtog: {type(resultat).__name__}."
        )

    forventede_felter = {
        "dokumenttitel",
        "forsendelsestype",
        "hoveddokument",
        "bilag",
        "faktisk_hoveddokument",
        "faktisk_bilag",
        "hoveddokumentvaelger_rækker",
        "bilagsvaelger_rækker",
        "test",
        "sendt",
    }

    manglende_felter = forventede_felter - resultat.keys()

    if manglende_felter:
        raise WorkItemError(
            "Digital Post-resultatet mangler felter. "
            f"Skade-id: {skade_id}. "
            "Manglende felter: "
            f"{sorted(manglende_felter)!r}."
        )

    if resultat.get("test") is not test:
        raise WorkItemError(
            "Digital Post-resultatets testværdi "
            "matcher ikke konfigurationen. "
            f"Skade-id: {skade_id}. "
            f"Forventede: {test}. "
            f"Modtog: {resultat.get('test')!r}."
        )

    forventet_sendt = not test

    if resultat.get("sendt") is not forventet_sendt:
        raise WorkItemError(
            "Digital Post-resultatets sendestatus "
            "er inkonsistent. "
            f"Skade-id: {skade_id}. "
            f"Testtilstand: {test}. "
            f"Forventede sendt: {forventet_sendt}. "
            f"Modtog: {resultat.get('sendt')!r}."
        )

    forventede_tekster = {
        "dokumenttitel": dokumenttitel,
        "forsendelsestype": forsendelsestype,
        "hoveddokument": hoveddokument_navn,
        "bilag": bilag_navn,
    }

    for feltnavn, forventet in forventede_tekster.items():
        faktisk = resultat.get(feltnavn)

        if not _tekster_er_ens(
            faktisk,
            forventet,
        ):
            raise WorkItemError(
                "Digital Post-resultatet indeholder "
                "en uventet værdi. "
                f"Felt: {feltnavn}. "
                f"Forventede: {forventet!r}. "
                f"Modtog: {faktisk!r}. "
                f"Skade-id: {skade_id}."
            )

    for feltnavn in (
        "faktisk_hoveddokument",
        "faktisk_bilag",
    ):
        value = resultat.get(feltnavn)

        if not isinstance(value, str) or not value.strip():
            raise WorkItemError(
                "Digital Post-resultatet mangler et "
                "bekræftet dokumentnavn. "
                f"Felt: {feltnavn}. "
                f"Skade-id: {skade_id}."
            )


# ------------------------------------------------------------
# AKTUEL STATUS
# ------------------------------------------------------------


def _hent_aktuel_status(
    *,
    skade: dict[str, Any],
) -> str:
    """Henter skadens aktuelle status."""
    value = (
        skade.get("status")
        or skade.get("IncidentStatus")
        or skade.get("incidentStatus")
    )

    if value is None:
        value = _find_felt_rekursivt(
            data=skade,
            feltnavne=STATUS_FELTER,
        )

    if value is None:
        raise WorkItemError(
            f"Det aktuelle skadesvar mangler status. Skade-id: {skade.get('id')!r}."
        )

    value = _udpak_visningsvaerdi(value)

    return _normaliser_paakraevet_tekst(
        value=value,
        field_name="skade.status",
    )


def _hent_statusser_der_ikke_maa_behandles() -> frozenset[str]:
    """Henter statusser, som ikke må behandles."""
    configured_statuses = getattr(
        configuration,
        "STATUSSER_DER_SKAL_FJERNES",
        None,
    )

    if configured_statuses is None:
        raise WorkItemError("configuration.STATUSSER_DER_SKAL_FJERNES mangler.")

    if isinstance(
        configured_statuses,
        str,
    ):
        configured_statuses = (configured_statuses,)

    try:
        normalized_statuses = frozenset(
            normalized_status
            for status in configured_statuses
            if (normalized_status := _normaliser_tekst(status))
        )
    except TypeError as error:
        raise WorkItemError(
            "configuration.STATUSSER_DER_SKAL_FJERNES skal være en samling af tekstværdier."
        ) from error

    if not normalized_statuses:
        raise WorkItemError("configuration.STATUSSER_DER_SKAL_FJERNES må ikke være tom.")

    return normalized_statuses


# ------------------------------------------------------------
# STANDARD CASE
# ------------------------------------------------------------


def _hent_standard_case(
    *,
    skade: dict[str, Any],
) -> bool:
    """Henter standardCase fra det aktuelle skadesvar."""
    value = skade.get("standardCase")

    if value is None:
        value = _find_felt_rekursivt(
            data=skade,
            feltnavne=STANDARD_CASE_FELTER,
        )

    value = _udpak_visningsvaerdi(value)

    normalized_value = _normaliser_bool(
        value=value,
    )

    if normalized_value is not None:
        return normalized_value

    raise WorkItemError(
        "Det aktuelle skadesvar mangler en gyldig "
        "standardCase-værdi. "
        f"Skade-id: {skade.get('id')!r}."
    )


def _hent_standard_case_fra_box(
    *,
    box: dict[str, Any],
) -> bool | None:
    """Henter standardCase fra work itemets box."""
    field_name = _find_box_feltnavn(
        box=box,
        feltnavne=STANDARD_CASE_FELTER,
    )

    if field_name is None:
        return None

    return _normaliser_bool(
        value=box.get(field_name),
    )


def _opdater_standard_case_i_box(
    *,
    data: dict[str, Any],
    item: Any,
    standard_case: bool,
) -> None:
    """Opdaterer standardCase i work itemets box."""
    update_item_data(
        data,
        item=item,
        box_updates={
            "standardCase": standard_case,
        },
    )


# ------------------------------------------------------------
# SKADEDATA
# ------------------------------------------------------------


def _kontroller_skade_id(
    *,
    skade: dict[str, Any],
    forventet_skade_id: int,
) -> None:
    """Kontrollerer, at API-svaret vedrører den rigtige skade."""
    if not isinstance(skade, dict):
        raise WorkItemError(
            f"Skadesvaret skal være en dictionary. Modtog: {type(skade).__name__}."
        )

    response_id = skade.get("id") or skade.get("Id")

    if response_id is None:
        raise WorkItemError(
            f"Skadesvaret mangler feltet id. Forventet skade-id: {forventet_skade_id}."
        )

    normalized_response_id = _normaliser_positivt_heltal(
        value=response_id,
        field_name="skade.id",
    )

    if normalized_response_id != forventet_skade_id:
        raise WorkItemError(
            "Insubiz returnerede en anden skade "
            "end den forespurgte. "
            f"Forventede: {forventet_skade_id}. "
            f"Modtog: {normalized_response_id}."
        )


def _hent_cpr_nummer(
    *,
    skade: dict[str, Any],
) -> str:
    """Finder og validerer CPR-nummeret i skadedata."""
    if not isinstance(skade, dict):
        raise WorkItemError(
            f"Skadesvaret skal være en dictionary. Modtog: {type(skade).__name__}."
        )

    claim_parties = skade.get("claimParties")

    if isinstance(claim_parties, list):
        for claim_part in claim_parties:
            if not isinstance(
                claim_part,
                dict,
            ):
                continue

            claim_part_type = claim_part.get("claimPartType")

            claim_part_type_text = _udpak_visningsvaerdi(claim_part_type)

            if not _tekster_er_ens(
                claim_part_type_text,
                "Skadelidte",
            ):
                continue

            cpr_nummer = _normaliser_cpr_nummer(
                value=(
                    claim_part.get("vat_Ni_Number") or claim_part.get("vatNiNumber")
                ),
            )

            if cpr_nummer:
                return cpr_nummer

    easy = skade.get("easy")

    if isinstance(easy, dict):
        claim_part = easy.get("claimPart")

        if isinstance(claim_part, dict):
            cpr_nummer = _normaliser_cpr_nummer(
                value=(
                    claim_part.get("vat_Ni_Number") or claim_part.get("vatNiNumber")
                ),
            )

            if cpr_nummer:
                return cpr_nummer

    direkte_felter = (
        "cpr",
        "cprNumber",
        "cpr_number",
        "socialSecurityNumber",
        "civilRegistrationNumber",
        "injuredPersonCpr",
    )

    for feltnavn in direkte_felter:
        cpr_nummer = _normaliser_cpr_nummer(
            value=skade.get(feltnavn),
        )

        if cpr_nummer:
            return cpr_nummer

    indlejrede_felter = (
        "injuredPerson",
        "injured",
        "person",
        "claimant",
        "employee",
        "personalInjury",
    )

    for feltnavn in indlejrede_felter:
        value = skade.get(feltnavn)

        if not isinstance(value, dict):
            continue

        for cpr_feltnavn in direkte_felter:
            cpr_nummer = _normaliser_cpr_nummer(
                value=value.get(cpr_feltnavn),
            )

            if cpr_nummer:
                return cpr_nummer

    dynamiske_felter = skade.get("dynamicFields")

    if isinstance(dynamiske_felter, list):
        for field in dynamiske_felter:
            if not isinstance(field, dict):
                continue

            field_name = str(
                field.get("name") or field.get("label") or field.get("key") or ""
            ).strip()

            if "cpr" not in field_name.casefold():
                continue

            field_value = field.get("value") or field.get("text")

            cpr_nummer = _normaliser_cpr_nummer(
                value=field_value,
            )

            if cpr_nummer:
                return cpr_nummer

    raise WorkItemError(
        "Skadesvaret indeholder ikke et læsbart "
        "CPR-nummer. "
        "Tilgængelige topfelter: "
        f"{list(skade.keys())!r}."
    )


def _normaliser_cpr_nummer(
    *,
    value: Any,
) -> str:
    """Normaliserer et CPR-nummer uden at logge værdien."""
    if value is None:
        return ""

    normalized_value = "".join(
        character for character in str(value) if character.isdigit()
    )

    if len(normalized_value) != 10:
        return ""

    return normalized_value


# ------------------------------------------------------------
# REKURSIVT FELTOPSLAG
# ------------------------------------------------------------


def _find_felt_rekursivt(
    *,
    data: Any,
    feltnavne: tuple[str, ...],
) -> Any:
    """Finder første match i dictionaries og lister."""
    normaliserede_feltnavne = {_normaliser_feltnavn(feltnavn) for feltnavn in feltnavne}

    if isinstance(data, dict):
        for key, value in data.items():
            if (
                isinstance(key, str)
                and _normaliser_feltnavn(key) in normaliserede_feltnavne
            ):
                return value

        for value in data.values():
            fundet = _find_felt_rekursivt(
                data=value,
                feltnavne=feltnavne,
            )

            if fundet is not None:
                return fundet

    elif isinstance(data, list):
        for value in data:
            fundet = _find_felt_rekursivt(
                data=value,
                feltnavne=feltnavne,
            )

            if fundet is not None:
                return fundet

    return None


def _udpak_visningsvaerdi(
    value: Any,
) -> Any:
    """Udpakker almindelige Insubiz-visningsobjekter."""
    if not isinstance(value, dict):
        return value

    for key in (
        "text",
        "name",
        "value",
        "displayValue",
        "display_value",
        "label",
        "number",
        "id",
    ):
        candidate = value.get(key)

        if candidate not in (
            None,
            "",
        ):
            return candidate

    return None


# ------------------------------------------------------------
# STATE
# ------------------------------------------------------------


def _hent_states(
    *,
    data: dict[str, Any],
) -> list[Any]:
    """Returnerer work itemets validerede state-liste."""
    states = data.get(
        "state",
        [],
    )

    if states is None:
        states = []
        data["state"] = states

    if not isinstance(states, list):
        raise WorkItemError(
            "Work item-feltet state skal være en liste. "
            f"Modtog: {type(states).__name__}."
        )

    return states


def _har_afsluttende_state(
    *,
    data: dict[str, Any],
) -> bool:
    """Kontrollerer, om itemet allerede er afsluttet."""
    return any(
        expected_state in str(existing_state)
        for existing_state in _hent_states(data=data)
        for expected_state in AFSLUTTENDE_STATES
    )


def _har_state(
    *,
    data: dict[str, Any],
    state: str,
) -> bool:
    """Kontrollerer, om en konkret state findes."""
    return any(
        state in str(existing_state) for existing_state in _hent_states(data=data)
    )


def _registrer_state(
    *,
    data: dict[str, Any],
    item: Any,
    state: str,
) -> None:
    """Registrerer en state uden dubletter."""
    if _har_state(
        data=data,
        state=state,
    ):
        logger.info(
            "State findes allerede: %s.",
            state,
        )

        return

    update_item_data(
        data,
        item=item,
        state=state,
    )


# ------------------------------------------------------------
# WORK ITEM-DATA
# ------------------------------------------------------------


def _hent_box(
    *,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Returnerer work itemets box."""
    box = data.get("box")

    if not isinstance(box, dict):
        raise WorkItemError(
            "Work item-feltet box skal være "
            "en dictionary. "
            f"Modtog: {type(box).__name__}."
        )

    return box


def _hent_skade_id_fra_box(
    *,
    box: dict[str, Any],
) -> int:
    """Henter og validerer skade-id fra box."""
    field_name = _find_box_feltnavn(
        box=box,
        feltnavne=SKADE_ID_FELTER,
    )

    if field_name is None:
        raise WorkItemError(
            "Work itemets box mangler skade-id. "
            "Forventede et af: "
            f"{list(SKADE_ID_FELTER)!r}. "
            "Tilgængelige felter: "
            f"{list(box.keys())!r}."
        )

    return _normaliser_positivt_heltal(
        value=box.get(field_name),
        field_name="box.Skade_id",
    )


def _hent_skade_nr_fra_box(
    *,
    box: dict[str, Any],
) -> str:
    """Henter og validerer skadenummer fra box."""
    field_name = _find_box_feltnavn(
        box=box,
        feltnavne=SKADE_NR_FELTER,
    )

    if field_name is None:
        raise WorkItemError(
            "Work itemets box mangler skadenummer. "
            "Forventede et af: "
            f"{list(SKADE_NR_FELTER)!r}. "
            "Tilgængelige felter: "
            f"{list(box.keys())!r}."
        )

    return _normaliser_paakraevet_tekst(
        value=box.get(field_name),
        field_name="box.Skade_nr",
    )


def _find_box_feltnavn(
    *,
    box: dict[str, Any],
    feltnavne: tuple[str, ...],
) -> str | None:
    """Finder et faktisk box-feltnavn via aliaser."""
    normaliserede_felter = {
        _normaliser_feltnavn(field_name): field_name
        for field_name in box
        if isinstance(field_name, str)
    }

    for feltnavn in feltnavne:
        faktisk_feltnavn = normaliserede_felter.get(_normaliser_feltnavn(feltnavn))

        if faktisk_feltnavn is not None:
            return faktisk_feltnavn

    return None


# ------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------


def _hent_digital_post_test() -> bool:
    """Henter Digital Post-testindstillingen."""
    value = getattr(
        configuration,
        "DIGITAL_POST_TEST",
        None,
    )

    if not isinstance(value, bool):
        raise WorkItemError(
            "configuration.DIGITAL_POST_TEST skal være boolsk. "
            f"Modtog: {type(value).__name__}."
        )

    return value


def _hent_konfigurationstekst(
    *,
    attribute_name: str,
    field_name: str,
) -> str:
    """Henter og validerer en tekstværdi fra configuration."""
    value = getattr(
        configuration,
        attribute_name,
        None,
    )

    if value is None:
        raise WorkItemError(f"Processens konfiguration mangler configuration.{field_name}.")

    return _normaliser_paakraevet_tekst(
        value=value,
        field_name=f"configuration.{field_name}",
    )


# ------------------------------------------------------------
# GENERELLE HJÆLPERE
# ------------------------------------------------------------


def _normaliser_bool(
    *,
    value: Any,
) -> bool | None:
    """Normaliserer almindelige boolske svarformer."""
    value = _udpak_visningsvaerdi(value)

    if isinstance(value, bool):
        return value

    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value
        in {
            0,
            1,
        }
    ):
        return bool(value)

    if isinstance(value, str):
        normalized_value = value.strip().casefold()

        if normalized_value in {
            "true",
            "1",
            "ja",
            "yes",
        }:
            return True

        if normalized_value in {
            "false",
            "0",
            "nej",
            "no",
        }:
            return False

    return None


def _normaliser_positivt_heltal(
    *,
    value: Any,
    field_name: str,
) -> int:
    """Normaliserer en værdi til et positivt heltal."""
    if isinstance(value, bool):
        raise WorkItemError(f"{field_name} må ikke være boolsk.")

    if isinstance(value, int):
        result = value

    elif isinstance(value, float):
        if not value.is_integer():
            raise WorkItemError(
                f"{field_name} indeholder decimaler. Modtog: {value!r}."
            )

        result = int(value)

    elif isinstance(value, str):
        normalized_value = value.strip()

        normalized_value = normalized_value.removesuffix(".0")

        if not normalized_value.isdigit():
            raise WorkItemError(
                f"{field_name} har et ugyldigt format. Modtog: {value!r}."
            )

        result = int(normalized_value)

    else:
        raise WorkItemError(
            f"{field_name} har et ugyldigt format. Modtog: {type(value).__name__}."
        )

    if result <= 0:
        raise WorkItemError(f"{field_name} skal være større end 0. Modtog: {result}.")

    return result


def _normaliser_paakraevet_tekst(
    *,
    value: Any,
    field_name: str,
) -> str:
    """Normaliserer en obligatorisk tekstværdi."""
    value = _udpak_visningsvaerdi(value)

    if value is None:
        raise WorkItemError(f"{field_name} mangler.")

    normalized_value = str(value).strip()

    if not normalized_value:
        raise WorkItemError(f"{field_name} må ikke være tom.")

    return normalized_value


def _normaliser_tekst(
    value: Any,
) -> str:
    """Normaliserer tekst til robust sammenligning."""
    value = _udpak_visningsvaerdi(value)

    return " ".join(str(value if value is not None else "").strip().split()).casefold()


def _tekster_er_ens(
    value: Any,
    expected: Any,
) -> bool:
    """Sammenligner to tekster robust."""
    return _normaliser_tekst(value) == _normaliser_tekst(expected)


def _normaliser_feltnavn(
    value: str,
) -> str:
    """Normaliserer et feltnavn til sammenligning."""
    normalized_value = str(value).strip().casefold()

    for character in (
        "_",
        "-",
        ".",
        ":",
    ):
        normalized_value = normalized_value.replace(
            character,
            " ",
        )

    return " ".join(normalized_value.split())


# ------------------------------------------------------------
# BROWSER- OG KLIENTVALIDERING
# ------------------------------------------------------------


def _valider_browserobjekter(
    *,
    session: BrowserSession,
    page: Page,
) -> None:
    """Validerer browserobjekterne fra main.py."""
    if session is None:
        raise WorkItemError("BrowserSession mangler.")

    if page is None:
        raise WorkItemError("Playwright-siden mangler.")

    if page.is_closed():
        raise WorkItemError("Playwright-siden er lukket.")

    context = getattr(
        session,
        "context",
        None,
    )

    if context is None:
        raise WorkItemError("BrowserSession mangler en aktiv BrowserContext.")

    if page.context is not context:
        raise WorkItemError(
            "Playwright-siden tilhører ikke den "
            "BrowserContext, som BrowserSession ejer."
        )


def _valider_api_client(
    *,
    api_client: InsubizApiClient,
) -> None:
    """Validerer den delte Insubiz API-klient."""
    if api_client is None:
        raise WorkItemError("Insubiz API-klienten mangler.")

    get_method = getattr(
        api_client,
        "get",
        None,
    )

    if not callable(get_method):
        raise WorkItemError("Insubiz API-klienten mangler en callable get-metode.")


__all__ = [
    "behandel_page",
]
