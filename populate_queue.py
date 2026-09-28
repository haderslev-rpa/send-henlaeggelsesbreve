from __future__ import annotations

"""Fylder Automation Server-køen med relevante skader fra Insubiz.

Filen følger proces-skabelonens producer-del og ændrer ikke strukturen:

1. Hent skadelisten fra Insubiz.
2. Filtrér status, undertype og skadetype.
3. Kontrollér om skade-id allerede findes som reference i ATS-køen.
4. Hent skadedetaljer for nye kandidater.
5. Kontrollér ``standardCase``.
6. Kontrollér ATS-køen igen umiddelbart før ``add_item``.
7. Opret ét work item pr. ny skade.

Behandling af kø-itemet sker fortsat på behandel-siden.

Work item-reference:
    Skadens tekniske skade-id som tekst.

Work itemets box:
    {
        "Skade nr": "2026-001234",
        "Skade id": 2488985,
        "standardCase": true,
        "IncidentType": "Arbejdsskade",
        "IncidentSubType": "Arbejdsulykke"
    }

Det komplette skadesvar gemmes ikke i work itemet.
"""

import inspect
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from automation_server_client import Workqueue
from q_haderslev_vbo.automation_server.ats_is_item_in_queue import (
    is_item_in_queue,
)
from q_haderslev_vbo.automation_server.ats_update_item_data import (
    update_item_data,
)
from q_haderslev_vbo.playwright.playwright_run_recorder import (
    PlaywrightRunRecorder,
)
from q_insubiz.api_client import create_api_client
from q_insubiz.functionality.skader import (
    SKADER_LISTE,
    hent_skade_via_id,
)

import config

logger = logging.getLogger(__name__)
logging.getLogger("q_insubiz").setLevel(logging.WARNING)


# ============================================================
# BOX-FELTER
# ============================================================

BOX_SKADE_NR = "Skade nr"
BOX_SKADE_ID = "Skade id"
BOX_STANDARD_CASE = "standardCase"
BOX_INCIDENT_TYPE = "IncidentType"
BOX_INCIDENT_SUBTYPE = "IncidentSubType"

FORVENTEDE_BOX_FELTER = frozenset(
    {
        BOX_SKADE_NR,
        BOX_SKADE_ID,
        BOX_STANDARD_CASE,
        BOX_INCIDENT_TYPE,
        BOX_INCIDENT_SUBTYPE,
    }
)


# ============================================================
# RESULTATMODEL
# ============================================================


@dataclass(slots=True)
class PopulateQueueResultat:
    """Optælling for én producer-kørsel."""

    skader_laest: int = 0
    fravalgt_status: int = 0
    fravalgt_undertype: int = 0
    fravalgt_type: int = 0
    allerede_i_koe: int = 0
    kandidater_efter_listefiltre: int = 0
    fravalgt_standard_case: int = 0
    lagt_i_koe: int = 0


# ============================================================
# NORMALISERING
# ============================================================


def _normaliser_feltnavn(value: Any) -> str:
    """Normaliserer et feltnavn til robust sammenligning."""
    normaliseret = str(value).strip().casefold()

    for tegn in ("_", "-", ".", ":", "/"):
        normaliseret = normaliseret.replace(tegn, " ")

    return " ".join(normaliseret.split())


def _normaliser_tekst(value: Any) -> str:
    """Normaliserer en Insubiz-værdi til sammenligning."""
    value = _udpak_visningsvaerdi(value)

    if value is None:
        return ""

    return " ".join(str(value).strip().split()).casefold()


def _normaliser_bool(value: Any) -> bool | None:
    """Normaliserer bool, 0/1 og almindelige boolske tekster."""
    value = _udpak_visningsvaerdi(value)

    if isinstance(value, bool):
        return value

    if isinstance(value, int) and not isinstance(value, bool):
        if value in {0, 1}:
            return bool(value)
        return None

    if isinstance(value, str):
        normaliseret = value.strip().casefold()

        if normaliseret in {"true", "1", "ja", "yes"}:
            return True

        if normaliseret in {"false", "0", "nej", "no"}:
            return False

    return None


def _normaliser_paakraevet_tekst(
    *,
    value: Any,
    feltnavn: str,
) -> str:
    """Returnerer en obligatorisk tekstværdi."""
    value = _udpak_visningsvaerdi(value)

    if value is None:
        raise ValueError(f"{feltnavn} mangler.")

    normaliseret = str(value).strip()

    if not normaliseret:
        raise ValueError(f"{feltnavn} må ikke være tom.")

    return normaliseret


def _udpak_visningsvaerdi(value: Any) -> Any:
    """Udpakker almindelige Insubiz-objekter uden at miste talværdier."""
    if not isinstance(value, Mapping):
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
        if candidate not in (None, ""):
            return candidate

    return None


# ============================================================
# FELTOPSLAG
# ============================================================


def _hent_felt(
    data: Mapping[str, Any],
    *feltnavne: str,
) -> Any:
    """Finder et felt direkte eller ved normaliseret feltnavn."""
    if not isinstance(data, Mapping):
        raise TypeError(
            f"data skal være dictionary-lignende. Modtog: {type(data).__name__}."
        )

    for feltnavn in feltnavne:
        if feltnavn in data:
            return data[feltnavn]

    normaliserede_felter = {
        _normaliser_feltnavn(key): value for key, value in data.items()
    }

    for feltnavn in feltnavne:
        normaliseret_feltnavn = _normaliser_feltnavn(feltnavn)
        if normaliseret_feltnavn in normaliserede_felter:
            return normaliserede_felter[normaliseret_feltnavn]

    return None


def _find_felt_rekursivt(
    data: Any,
    *feltnavne: str,
) -> Any:
    """Finder første match i nested dictionaries og lister."""
    normaliserede_navne = {_normaliser_feltnavn(name) for name in feltnavne}

    if isinstance(data, Mapping):
        for key, value in data.items():
            if _normaliser_feltnavn(key) in normaliserede_navne:
                return value

        for value in data.values():
            fundet = _find_felt_rekursivt(value, *feltnavne)
            if fundet is not None:
                return fundet

    elif isinstance(data, list):
        for value in data:
            fundet = _find_felt_rekursivt(value, *feltnavne)
            if fundet is not None:
                return fundet

    return None


def _tilgaengelige_felter(data: Mapping[str, Any]) -> list[str]:
    """Returnerer kun feltnavne til sikker fejlsøgning."""
    return sorted(str(key) for key in data)


# ============================================================
# SKADEFELTER
# ============================================================


def _hent_skade_id(skade: Mapping[str, Any]) -> int:
    """Henter og validerer skadens tekniske id."""
    value = _hent_felt(
        skade,
        "Id",
        "id",
        "Skade id",
        "Skade-id",
        "Skade_id",
        "skade_id",
        "IncidentId",
        "incidentId",
    )

    if isinstance(value, bool):
        raise TypeError("Skade-id må ikke være boolsk.")

    value = _udpak_visningsvaerdi(value)

    try:
        skade_id = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(
            "Skadelisterækken mangler et gyldigt skade-id. "
            f"Modtog: {value!r}. "
            f"Tilgængelige felter: {_tilgaengelige_felter(skade)!r}."
        ) from error

    if skade_id <= 0:
        raise ValueError(f"Skade-id skal være større end 0. Modtog: {skade_id}.")

    return skade_id


def _hent_skade_nr(
    *,
    skade_fra_liste: Mapping[str, Any],
    skade_detaljer: Mapping[str, Any],
    skade_id: int,
) -> str:
    """Henter skadenummer fra liste eller detaljer.

    Den tidligere fungerende implementering understøttede flere Insubiz-navne.
    De er alle bevaret, og detaljesvaret bruges som fallback. Skadenummeret
    hentes først efter ``hent_skade_via_id``, så en manglende listekolonne ikke
    længere stopper populationen.
    """
    aliases = (
        "IncidentNumberInternal",
        "incidentNumberInternal",
        "Incident number internal",
        "Incident Number Internal",
        "IncidentNumber",
        "incidentNumber",
        "Skade nr",
        "Skade nr.",
        "Skade-nr",
        "Skade_nr",
        "Skadenr",
        "Skadenr.",
        "Skadenummer",
        "Skade nummer",
        "ClaimNumber",
        "claimNumber",
    )

    value = _hent_felt(skade_fra_liste, *aliases)

    if value in (None, ""):
        value = _find_felt_rekursivt(skade_detaljer, *aliases)

    if _normaliser_tekst(value):
        return _normaliser_paakraevet_tekst(
            value=value,
            feltnavn=BOX_SKADE_NR,
        )

    raise ValueError(
        "Skade nr mangler både i skadelisten og skadedetaljerne. "
        f"Skade-id: {skade_id}. "
        "Skadelistefelter: "
        f"{_tilgaengelige_felter(skade_fra_liste)!r}. "
        "Topfelter i skadedetaljer: "
        f"{_tilgaengelige_felter(skade_detaljer)!r}."
    )


def _hent_incident_type(
    *,
    skade_fra_liste: Mapping[str, Any],
    skade_detaljer: Mapping[str, Any],
) -> str:
    aliases = (
        "IncidentType",
        "incidentType",
        "Incident Type",
        "Skadetype",
        "Skade type",
        "type",
    )

    value = _hent_felt(skade_fra_liste, *aliases)
    if value in (None, ""):
        value = _find_felt_rekursivt(skade_detaljer, *aliases)

    return _normaliser_paakraevet_tekst(
        value=value,
        feltnavn=BOX_INCIDENT_TYPE,
    )


def _hent_incident_subtype(
    *,
    skade_fra_liste: Mapping[str, Any],
    skade_detaljer: Mapping[str, Any],
) -> str:
    aliases = (
        "IncidentSubType",
        "incidentSubType",
        "Incident Sub Type",
        "Undertype",
        "subType",
    )

    value = _hent_felt(skade_fra_liste, *aliases)
    if value in (None, ""):
        value = _find_felt_rekursivt(skade_detaljer, *aliases)

    return _normaliser_paakraevet_tekst(
        value=value,
        feltnavn=BOX_INCIDENT_SUBTYPE,
    )


def _hent_standard_case(skade_detaljer: Mapping[str, Any]) -> bool:
    """Henter standardCase rekursivt fra detaljesvaret."""
    value = _find_felt_rekursivt(
        skade_detaljer,
        "standardCase",
        "StandardCase",
        "standard case",
        "Standard case",
    )

    normaliseret = _normaliser_bool(value)

    if normaliseret is None:
        raise ValueError(
            "Skaden mangler en gyldig boolsk standardCase-værdi. "
            f"Topfelter: {_tilgaengelige_felter(skade_detaljer)!r}."
        )

    return normaliseret


# ============================================================
# STATUS- OG LISTEFILTRE
# ============================================================


def _hent_statusser_der_skal_fjernes() -> frozenset[str]:
    """Henter statusserne fra den nye eller tidligere config-kontrakt."""
    configured_statuses = getattr(
        config,
        "STATUSSER_DER_SKAL_FJERNES",
        None,
    )

    if configured_statuses is None:
        legacy_status = getattr(
            config,
            "STATUS_DER_SKAL_FJERNES",
            None,
        )
        configured_statuses = (legacy_status,) if legacy_status is not None else ()

    if isinstance(configured_statuses, str):
        configured_statuses = (configured_statuses,)

    if not isinstance(configured_statuses, Iterable):
        raise TypeError("STATUSSER_DER_SKAL_FJERNES skal være en samling af tekst.")

    normaliserede = frozenset(
        _normaliser_tekst(status)
        for status in configured_statuses
        if _normaliser_tekst(status)
    )

    if not normaliserede:
        raise RuntimeError(
            "Der er ikke konfigureret statusser, som skal filtreres fra."
        )

    return normaliserede


def _status_skal_beholdes(skade: Mapping[str, Any]) -> bool:
    """Frasorterer blandt andet Afsluttet og Genoptaget."""
    status = _hent_felt(
        skade,
        "IncidentStatus",
        "incidentStatus",
        "Incident Status",
        "Status",
        "status",
    )

    return _normaliser_tekst(status) not in _hent_statusser_der_skal_fjernes()


def _undertype_skal_beholdes(skade: Mapping[str, Any]) -> bool:
    undertype = _hent_felt(
        skade,
        "IncidentSubType",
        "incidentSubType",
        "Incident Sub Type",
        "Undertype",
        "subType",
    )

    return _normaliser_tekst(undertype) == _normaliser_tekst(
        config.INCIDENT_SUBTYPE_DER_SKAL_BEHOLDES
    )


def _type_skal_beholdes(skade: Mapping[str, Any]) -> bool:
    skadetype = _hent_felt(
        skade,
        "IncidentType",
        "incidentType",
        "Incident Type",
        "Skadetype",
        "Skade type",
        "type",
    )

    return _normaliser_tekst(skadetype) == _normaliser_tekst(
        config.INCIDENT_TYPE_DER_SKAL_BEHOLDES
    )


# ============================================================
# ATS-DUBLETKONTROL
# ============================================================


def _hent_queue_id() -> int:
    queue_id = getattr(config, "QUEUE_ID", None)

    if isinstance(queue_id, bool):
        raise TypeError("config.QUEUE_ID må ikke være boolsk.")

    try:
        normaliseret_queue_id = int(queue_id)
    except (TypeError, ValueError) as error:
        raise TypeError(
            "config.QUEUE_ID skal være et heltal eller tekst med et heltal."
        ) from error

    if normaliseret_queue_id <= 0:
        raise ValueError("config.QUEUE_ID skal være større end 0.")

    return normaliseret_queue_id


def _item_findes_i_koeen(*, skade_id: int) -> bool:
    """Søger reference i alle fem ATS-statusser og hele historikken."""
    return is_item_in_queue(
        queue_id=_hent_queue_id(),
        item_reference=str(skade_id),
    )


# ============================================================
# WORK ITEM-DATA
# ============================================================


def _opret_work_item_data(
    *,
    skade_id: int,
    skade_nr: str,
    standard_case: bool,
    incident_type: str,
    incident_subtype: str,
) -> dict[str, Any]:
    """Opretter work item-data med præcis fem felter i box."""
    data_json: dict[str, Any] = {
        "box": {},
        "defer": None,
        "state": [],
        "status": {},
    }

    box_updates = {
        BOX_SKADE_NR: _normaliser_paakraevet_tekst(
            value=skade_nr,
            feltnavn=BOX_SKADE_NR,
        ),
        BOX_SKADE_ID: skade_id,
        BOX_STANDARD_CASE: standard_case,
        BOX_INCIDENT_TYPE: _normaliser_paakraevet_tekst(
            value=incident_type,
            feltnavn=BOX_INCIDENT_TYPE,
        ),
        BOX_INCIDENT_SUBTYPE: _normaliser_paakraevet_tekst(
            value=incident_subtype,
            feltnavn=BOX_INCIDENT_SUBTYPE,
        ),
    }

    update_item_data(
        data_json,
        box_updates=box_updates,
        update=False,
    )

    box = data_json.get("box")

    if not isinstance(box, dict):
        raise TypeError("update_item_data oprettede ikke en gyldig box.")

    faktiske_felter = frozenset(box.keys())

    if faktiske_felter != FORVENTEDE_BOX_FELTER:
        ekstra_felter = sorted(faktiske_felter - FORVENTEDE_BOX_FELTER)
        manglende_felter = sorted(FORVENTEDE_BOX_FELTER - faktiske_felter)

        raise RuntimeError(
            "Work itemets box har ikke den forventede struktur. "
            f"Ekstra felter: {ekstra_felter!r}. "
            f"Manglende felter: {manglende_felter!r}."
        )

    return data_json


async def _add_item(
    *,
    workqueue: Workqueue,
    data: dict[str, Any],
    reference: str,
) -> None:
    """Understøtter både nuværende synkrone og fremtidige awaitable klienter."""
    result = workqueue.add_item(
        data=data,
        reference=reference,
    )

    if inspect.isawaitable(result):
        await result


# ============================================================
# RAPPORTERING
# ============================================================


def _print_resultat(resultat: PopulateQueueResultat) -> None:
    print()
    print("=" * 80)
    print("POPULATE QUEUE AFSLUTTET")
    print("=" * 80)
    print(f"Skader læst fra Insubiz:                 {resultat.skader_laest}")
    print(f"Fravalgt status:                         {resultat.fravalgt_status}")
    print(f"Fravalgt forkert undertype:              {resultat.fravalgt_undertype}")
    print(f"Fravalgt forkert skadetype:              {resultat.fravalgt_type}")
    print(f"Allerede fundet i ATS-køen:              {resultat.allerede_i_koe}")
    print(
        "Skader efter liste- og dubletfiltre:      "
        f"{resultat.kandidater_efter_listefiltre}"
    )
    print(
        f"Fravalgt standardCase:                    {resultat.fravalgt_standard_case}"
    )
    print(f"Nye items lagt i køen:                    {resultat.lagt_i_koe}")
    print("=" * 80)


# ============================================================
# PUBLIC PRODUCER-FUNKTION
# ============================================================


async def populate_queue(
    workqueue: Workqueue,
    debug: bool = False,
    recorder: PlaywrightRunRecorder | None = None,
) -> int:
    """Henter, filtrerer og lægger kun nye skader i workqueuen.

    Dubletkontrollen udføres både før detailkaldet og lige før add_item.
    Funktionen returnerer antallet af nye items, som faktisk blev oprettet.
    """
    if workqueue is None:
        raise ValueError("workqueue må ikke være None.")

    if not isinstance(debug, bool):
        raise TypeError("debug skal være True eller False.")

    api_client = create_api_client(
        headless=config.HEADLESS,
        debug=debug,
        recorder=recorder,
    )

    resultat = PopulateQueueResultat()

    try:
        skader = await SKADER_LISTE(
            api_client=api_client,
            customer_id=config.CUSTOMER_ID,
            customer_segmentation_1=(config.CUSTOMER_SEGMENTATION_1),
            customer_segmentation_2=(config.CUSTOMER_SEGMENTATION_2),
            claim_group_id=config.CLAIM_GROUP_ID,
            status_id=config.STATUS_ID,
            created_year_from=config.CREATED_YEAR_FROM,
            created_year_to=config.CREATED_YEAR_TO,
            incident_year_from=config.INCIDENT_YEAR_FROM,
            incident_year_to=config.INCIDENT_YEAR_TO,
            show_tree_data=config.SHOW_TREE_DATA,
            columns=config.SKADELISTE_COLUMNS,
        )

        if not isinstance(skader, list):
            raise TypeError(
                "SKADER_LISTE returnerede et uventet format. "
                f"Modtog: {type(skader).__name__}."
            )

        resultat.skader_laest = len(skader)
        kandidater: list[dict[str, Any]] = []

        for skade in skader:
            if not isinstance(skade, dict):
                logger.warning(
                    "En skadelisterække blev sprunget over, fordi rækken "
                    "ikke var en dictionary. Type: %s.",
                    type(skade).__name__,
                )
                continue

            if not _status_skal_beholdes(skade):
                resultat.fravalgt_status += 1
                continue

            if not _undertype_skal_beholdes(skade):
                resultat.fravalgt_undertype += 1
                continue

            if not _type_skal_beholdes(skade):
                resultat.fravalgt_type += 1
                continue

            skade_id = _hent_skade_id(skade)

            # Første dubletkontrol sparer et dyrere detaljekald til Insubiz.
            if _item_findes_i_koeen(skade_id=skade_id):
                resultat.allerede_i_koe += 1

                if debug:
                    logger.info(
                        "Skade findes allerede i ATS-køen. Skade-id: %s. Queue-id: %s.",
                        skade_id,
                        _hent_queue_id(),
                    )

                continue

            kandidater.append(skade)

        resultat.kandidater_efter_listefiltre = len(kandidater)

        for skade_fra_liste in kandidater:
            skade_id = _hent_skade_id(skade_fra_liste)

            skade_detaljer = await hent_skade_via_id(
                api_client=api_client,
                skade_id=skade_id,
            )

            if not isinstance(skade_detaljer, dict):
                raise TypeError(
                    "hent_skade_via_id returnerede et uventet format. "
                    f"Skade-id: {skade_id}. "
                    f"Modtog: {type(skade_detaljer).__name__}."
                )

            standard_case = _hent_standard_case(skade_detaljer)

            if standard_case is not config.STANDARD_CASE_SKAL_VAERE:
                resultat.fravalgt_standard_case += 1
                continue

            # Disse felter hentes først nu. Detaljesvaret fungerer som fallback,
            # hvis en kolonne ikke fandtes eller var tom i SKADER_LISTE.
            skade_nr = _hent_skade_nr(
                skade_fra_liste=skade_fra_liste,
                skade_detaljer=skade_detaljer,
                skade_id=skade_id,
            )
            incident_type = _hent_incident_type(
                skade_fra_liste=skade_fra_liste,
                skade_detaljer=skade_detaljer,
            )
            incident_subtype = _hent_incident_subtype(
                skade_fra_liste=skade_fra_liste,
                skade_detaljer=skade_detaljer,
            )

            # Anden kontrol reducerer kapløb mellem samtidige producer-kørsler.
            if _item_findes_i_koeen(skade_id=skade_id):
                resultat.allerede_i_koe += 1

                if debug:
                    logger.info(
                        "Skade blev fundet i ATS ved anden kontrol. Skade-id: %s.",
                        skade_id,
                    )

                continue

            data_json = _opret_work_item_data(
                skade_id=skade_id,
                skade_nr=skade_nr,
                standard_case=standard_case,
                incident_type=incident_type,
                incident_subtype=incident_subtype,
            )

            await _add_item(
                workqueue=workqueue,
                data=data_json,
                reference=str(skade_id),
            )

            resultat.lagt_i_koe += 1

            if debug:
                logger.info(
                    "Skade lagt i kø. Skade-id: %s. Skade nr: %s.",
                    skade_id,
                    skade_nr,
                )

    finally:
        await api_client.close()

    logger.info(
        "Skader læst: %s. Fravalgt status: %s. "
        "Fravalgt undertype: %s. Fravalgt type: %s. "
        "Allerede i kø: %s. Kandidater: %s. "
        "Fravalgt standardCase: %s. Nye items: %s.",
        resultat.skader_laest,
        resultat.fravalgt_status,
        resultat.fravalgt_undertype,
        resultat.fravalgt_type,
        resultat.allerede_i_koe,
        resultat.kandidater_efter_listefiltre,
        resultat.fravalgt_standard_case,
        resultat.lagt_i_koe,
    )

    _print_resultat(resultat)
    return resultat.lagt_i_koe


__all__ = [
    "PopulateQueueResultat",
    "populate_queue",
]
