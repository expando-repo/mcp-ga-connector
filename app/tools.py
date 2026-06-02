"""
Google Analytics nástroje pro MCP
Každý tool odpovídá jednomu nástroji z google-analytics-mcp
"""
import json
import logging
from typing import Any
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


def _build_credentials(creds_dict: dict) -> Credentials:
    """Vytvoří Credentials objekt z uloženého slovníku a případně refreshne token."""
    from app.config import settings

    creds = Credentials(
        token=creds_dict["token"],
        refresh_token=creds_dict.get("refresh_token"),
        token_uri=creds_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=creds_dict.get("scopes", []),
        expiry=creds_dict.get("token_expiry"),
    )

    # Refresh pokud expiroval (expiry je naive UTC datetime, jak ho vrací Google).
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleRequest())

    return creds


# ---------------------------------------------------------------------------
# Definice nástrojů (posílá se Claude jako tools/list odpověď)
# ---------------------------------------------------------------------------

def get_tools_definition() -> list[dict]:
    return [
        {
            "name": "get_account_summaries",
            "description": (
                "Vrátí přehled všech Google Analytics účtů a properties, "
                "ke kterým má přihlášený uživatel přístup."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        {
            "name": "get_property_details",
            "description": "Vrátí podrobné informace o konkrétní GA4 property.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "property_id": {
                        "type": "string",
                        "description": "ID property ve formátu 'properties/123456789'",
                    }
                },
                "required": ["property_id"],
            },
        },
        {
            "name": "run_report",
            "description": (
                "Spustí Google Analytics report pomocí Data API. "
                "Umožňuje dotazovat metriky (sessions, users, pageviews, ...) "
                "a dimenze (datum, země, kanál, ...) pro zvolenou property a časové období."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "property_id": {
                        "type": "string",
                        "description": "ID property, např. '123456789' (bez prefixu 'properties/')",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Počáteční datum ve formátu YYYY-MM-DD nebo '30daysAgo', '7daysAgo', 'yesterday'",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Koncové datum ve formátu YYYY-MM-DD nebo 'today'",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Seznam metrik, např. ['sessions', 'activeUsers', 'screenPageViews']",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Seznam dimenzí, např. ['date', 'country', 'sessionDefaultChannelGroup']",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max počet řádků výsledku (výchozí: 100, max: 10000)",
                        "default": 100,
                    },
                },
                "required": ["property_id", "start_date", "end_date", "metrics"],
            },
        },
        {
            "name": "run_realtime_report",
            "description": "Spustí realtime report - zobrazí aktuálně aktivní uživatele na webu/aplikaci.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "property_id": {
                        "type": "string",
                        "description": "ID property (číslo bez 'properties/' prefixu)",
                    },
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Realtime metriky, např. ['activeUsers']",
                    },
                    "dimensions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Realtime dimenze, např. ['country', 'unifiedScreenName']",
                    },
                },
                "required": ["property_id", "metrics"],
            },
        },
        {
            "name": "get_custom_dimensions_and_metrics",
            "description": "Vrátí vlastní dimenze a metriky definované v dané GA4 property.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "property_id": {
                        "type": "string",
                        "description": "ID property ve formátu 'properties/123456789'",
                    }
                },
                "required": ["property_id"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Implementace nástrojů
# ---------------------------------------------------------------------------

async def handle_tool_call(tool_name: str, args: dict, credentials_dict: dict) -> Any:
    """Dispatchuje volání nástroje na správnou funkci."""
    creds = _build_credentials(credentials_dict)

    handlers = {
        "get_account_summaries": _get_account_summaries,
        "get_property_details": _get_property_details,
        "run_report": _run_report,
        "run_realtime_report": _run_realtime_report,
        "get_custom_dimensions_and_metrics": _get_custom_dimensions_and_metrics,
    }

    handler = handlers.get(tool_name)
    if not handler:
        raise ValueError(f"Neznámý nástroj: {tool_name}")

    return await handler(creds, args)


async def _get_account_summaries(creds: Credentials, args: dict) -> dict:
    """Vrátí seznam GA účtů a properties."""
    import asyncio

    def _call():
        service = build("analyticsadmin", "v1beta", credentials=creds)
        result = service.accountSummaries().list().execute()
        summaries = result.get("accountSummaries", [])
        output = []
        for account in summaries:
            output.append({
                "account": account.get("account"),
                "displayName": account.get("displayName"),
                "properties": [
                    {
                        "property": p.get("property"),
                        "displayName": p.get("displayName"),
                    }
                    for p in account.get("propertySummaries", [])
                ],
            })
        return {"accounts": output, "total": len(output)}

    return await asyncio.get_event_loop().run_in_executor(None, _call)


async def _get_property_details(creds: Credentials, args: dict) -> dict:
    """Vrátí detail property."""
    import asyncio

    property_id = args["property_id"]
    if not property_id.startswith("properties/"):
        property_id = f"properties/{property_id}"

    def _call():
        service = build("analyticsadmin", "v1beta", credentials=creds)
        prop = service.properties().get(name=property_id).execute()
        return prop

    return await asyncio.get_event_loop().run_in_executor(None, _call)


async def _run_report(creds: Credentials, args: dict) -> dict:
    """Spustí GA Data API report."""
    import asyncio

    property_id = args["property_id"]
    start_date = args["start_date"]
    end_date = args["end_date"]
    metrics = args["metrics"]
    dimensions = args.get("dimensions", [])
    limit = min(args.get("limit", 100), 10000)

    def _call():
        service = build("analyticsdata", "v1beta", credentials=creds)
        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "metrics": [{"name": m} for m in metrics],
            "dimensions": [{"name": d} for d in dimensions],
            "limit": limit,
        }
        response = service.properties().runReport(
            property=f"properties/{property_id}", body=body
        ).execute()

        # Zpracuj výsledky do čitelného formátu
        headers = (
            [h["name"] for h in response.get("dimensionHeaders", [])]
            + [h["name"] for h in response.get("metricHeaders", [])]
        )
        rows = []
        for row in response.get("rows", []):
            values = (
                [v["value"] for v in row.get("dimensionValues", [])]
                + [v["value"] for v in row.get("metricValues", [])]
            )
            rows.append(dict(zip(headers, values)))

        return {
            "rowCount": response.get("rowCount", 0),
            "rows": rows,
            "metadata": {
                "property": f"properties/{property_id}",
                "dateRange": {"start": start_date, "end": end_date},
            },
        }

    return await asyncio.get_event_loop().run_in_executor(None, _call)


async def _run_realtime_report(creds: Credentials, args: dict) -> dict:
    """Spustí realtime report."""
    import asyncio

    property_id = args["property_id"]
    metrics = args["metrics"]
    dimensions = args.get("dimensions", [])

    def _call():
        service = build("analyticsdata", "v1beta", credentials=creds)
        body = {
            "metrics": [{"name": m} for m in metrics],
            "dimensions": [{"name": d} for d in dimensions],
        }
        response = service.properties().runRealtimeReport(
            property=f"properties/{property_id}", body=body
        ).execute()

        headers = (
            [h["name"] for h in response.get("dimensionHeaders", [])]
            + [h["name"] for h in response.get("metricHeaders", [])]
        )
        rows = []
        for row in response.get("rows", []):
            values = (
                [v["value"] for v in row.get("dimensionValues", [])]
                + [v["value"] for v in row.get("metricValues", [])]
            )
            rows.append(dict(zip(headers, values)))

        return {"rowCount": response.get("rowCount", 0), "rows": rows}

    return await asyncio.get_event_loop().run_in_executor(None, _call)


async def _get_custom_dimensions_and_metrics(creds: Credentials, args: dict) -> dict:
    """Vrátí custom dimenze a metriky property."""
    import asyncio

    property_id = args["property_id"]
    if not property_id.startswith("properties/"):
        property_id = f"properties/{property_id}"

    def _call():
        service = build("analyticsadmin", "v1beta", credentials=creds)
        dims = service.properties().customDimensions().list(parent=property_id).execute()
        mets = service.properties().customMetrics().list(parent=property_id).execute()
        return {
            "customDimensions": dims.get("customDimensions", []),
            "customMetrics": mets.get("customMetrics", []),
        }

    return await asyncio.get_event_loop().run_in_executor(None, _call)
