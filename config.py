from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# ------------------------------------------------------------
# DATO
# ------------------------------------------------------------

LOCAL_TIMEZONE = ZoneInfo("Europe/Copenhagen")

CURRENT_YEAR: int = datetime.now(
    tz=LOCAL_TIMEZONE,
).year


# ------------------------------------------------------------
# BROWSER
# ------------------------------------------------------------

HEADLESS: bool = True


# ------------------------------------------------------------
# SKADELISTE
# ------------------------------------------------------------

CUSTOMER_ID: int | None = None

CUSTOMER_SEGMENTATION_1: int = -1
CUSTOMER_SEGMENTATION_2: int = -1

CLAIM_GROUP_ID: int = 0

STATUS_ID: int = -2

CREATED_YEAR_FROM: int = 0
CREATED_YEAR_TO: int = CURRENT_YEAR

INCIDENT_YEAR_FROM: int = CURRENT_YEAR - 1
INCIDENT_YEAR_TO: int = CURRENT_YEAR

SHOW_TREE_DATA: bool = False

SKADELISTE_COLUMNS: list[str] = [
    "Id",
    "IncidentNumberInternal",
    "IncidentType",
    "IncidentSubType",
    "IncidentStatus",
    "Created",
    "standardCase",
]


# ------------------------------------------------------------
# SKADEFILTRE
# ------------------------------------------------------------

STATUSSER_DER_SKAL_FJERNES: tuple[str, ...] = (
    "Afsluttet",
    "Genoptaget",
)

INCIDENT_SUBTYPE_DER_SKAL_BEHOLDES: str = "Arbejdsulykke"

INCIDENT_TYPE_DER_SKAL_BEHOLDES: str = "Arbejdsskade"

# Kun detailværdien True må komme i køen.

STANDARD_CASE_SKAL_VAERE: bool = True


# ------------------------------------------------------------
# DOKUMENT FRA SKABELON
# ------------------------------------------------------------

SKABELON_NAVN: str = "Robot - Henlæggelsesbrev"

DOKUMENTTITEL: str = "Brev fra Haderslev Kommune"

HOVEDDOKUMENT_NAVN: str = SKABELON_NAVN


# ------------------------------------------------------------
# DIGITAL POST
# ------------------------------------------------------------

FORSENDELSESTYPE: str = "Insubizbrev"

BILAG_NAVN: str = "Anmeldelse af arbejdsulykke"

# True:
# Digital Post-dialogen udfyldes, men Send-knappen klikkes ikke.
#
# False:
# Digital Post sendes reelt.

DIGITAL_POST_TEST: bool = False


# ------------------------------------------------------------
# AUTOMATION SERVER
# ------------------------------------------------------------

QUEUE_ID: int = 17
