from __future__ import annotations

import logging
from typing import Any

from automation_server_client import WorkItemError
from q_insubiz.api.client import InsubizApiClient
from q_insubiz.functionality.skader import (
    hent_skade_via_id,
    opdater_skade_status_fra_seneste_data,
)
from q_insubiz.models import SkadeStatus

import configuration
from behandel import (
    STATE_DIGITAL_POST_SENDT,
    STATE_KOMMENTAR_OPRETTET,
    _har_state,
    _hent_aktuel_status,
    _hent_box,
    _hent_skade_id_fra_box,
    _hent_standard_case,
    _hent_statusser_der_ikke_maa_behandles,
    _kontroller_skade_id,
    _normaliser_tekst,
    _registrer_state,
)

logger = logging.getLogger(__name__)

STATE_SKADE_AFSLUTTET = "5.0 Skade afsluttet i Insubiz"


def _hent_status_id(skade: dict[str, Any]) -> int:
    """Returnerer skadens status-id eller stopper ved ugyldige data."""
    status = skade.get("status")
    value = (
        status.get("id")
        if isinstance(status, dict)
        else skade.get("statusId")
    )

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise WorkItemError(
            "Skadesvaret mangler et gyldigt status-id."
        )

    try:
        return int(value)
    except ValueError as error:
        raise WorkItemError(
            "Skadesvaret indeholder et ugyldigt status-id."
        ) from error


async def afslut_skade_efter_brev(
    *,
    item: Any,
    api_client: InsubizApiClient,
) -> None:
    """
    Afslutter skaden efter brev og kommentar.

    Returnerer None, når Insubiz-status er bekræftet som Afsluttet,
    og afslutningsstate er gemt. Ved fejl afsluttes ATS-itemet ikke.
    """
    data = item.data

    if not isinstance(data, dict):
        raise WorkItemError("Work itemet mangler gyldige data.")

    if configuration.DIGITAL_POST_TEST is not False:
        raise WorkItemError(
            "Skaden afsluttes ikke i Digital Post-testtilstand."
        )

    # Begge handlinger skal være registreret før statusændringen.
    for state in (
        STATE_DIGITAL_POST_SENDT,
        STATE_KOMMENTAR_OPRETTET,
    ):
        if not _har_state(data=data, state=state):
            raise WorkItemError(
                f"Skaden afsluttes ikke: mangler state {state!r}."
            )

    skade_id = _hent_skade_id_fra_box(
        box=_hent_box(data=data),
    )

    skade = await hent_skade_via_id(
        api_client=api_client,
        skade_id=skade_id,
    )
    _kontroller_skade_id(
        skade=skade,
        forventet_skade_id=skade_id,
    )

    if _hent_status_id(skade) != int(SkadeStatus.AFSLUTTET):
        # En senere genåbning må ikke automatisk overskrives.
        if _har_state(data=data, state=STATE_SKADE_AFSLUTTET):
            raise WorkItemError(
                "Skaden har en afslutningsstate, men er ikke længere "
                "Afsluttet i Insubiz. Manuel kontrol kræves."
            )

        if not _hent_standard_case(skade=skade):
            raise WorkItemError(
                "Skaden er ikke længere standardCase. "
                "Manuel kontrol kræves før afslutning."
            )

        aktuel_status = _hent_aktuel_status(skade=skade)
        if (
            _normaliser_tekst(aktuel_status)
            in _hent_statusser_der_ikke_maa_behandles()
        ):
            raise WorkItemError(
                "Skadens aktuelle status tillader ikke afslutning "
                f"via robotten: {aktuel_status!r}."
            )

        logger.info(
            "Afslutter skade i Insubiz. Skade-id: %s.",
            skade_id,
        )

        await opdater_skade_status_fra_seneste_data(
            api_client=api_client,
            skade_id=skade_id,
            status=SkadeStatus.AFSLUTTET,
        )

        # Et gennemført API-kald er ikke nok: kontrollér gemt status.
        skade = await hent_skade_via_id(
            api_client=api_client,
            skade_id=skade_id,
        )
        _kontroller_skade_id(
            skade=skade,
            forventet_skade_id=skade_id,
        )

        if _hent_status_id(skade) != int(SkadeStatus.AFSLUTTET):
            raise WorkItemError(
                "Efterkontrollen bekræftede ikke Afsluttet i Insubiz. "
                f"Skade-id: {skade_id}. ATS-itemet afsluttes ikke."
            )

    # Hvis status allerede var Afsluttet, gemmes den manglende state.
    _registrer_state(
        data=data,
        item=item,
        state=STATE_SKADE_AFSLUTTET,
    )

    logger.info(
        "Skaden er bekræftet afsluttet i Insubiz. Skade-id: %s.",
        skade_id,
    )
    print(f"Skaden er afsluttet i Insubiz. Skade-id: {skade_id}")