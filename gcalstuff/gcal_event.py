#!/usr/bin/env python3
"""
Create Google Calendar events via CLI with day-view confirmation.

Authenticates via OAuth2 Desktop flow, lists existing events for the target day,
and requires explicit confirmation before creating the new event.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def parse_args() -> argparse.Namespace:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(
        description="Create a Google Calendar event with day-view confirmation"
    )
    parser.add_argument("title", help="Event title")
    parser.add_argument("date", help="Event date (YYYY-MM-DD)")
    parser.add_argument("start_time", help="Start time (HH:MM)")
    parser.add_argument(
        "-d", "--duration",
        type=int,
        default=60,
        help="Duration in minutes (default: 60)",
    )
    parser.add_argument(
        "--description",
        default="",
        help="Optional event description",
    )
    parser.add_argument(
        "--calendar-id",
        default="primary",
        help="Google Calendar ID (default: primary)",
    )
    parser.add_argument(
        "--credentials",
        default=str(Path.home() / ".config" / "gcal" / "credentials.json"),
        help="Path to OAuth2 credentials.json (default: ~/.config/gcal/credentials.json)",
    )
    parser.add_argument(
        "--token",
        default=str(Path.home() / ".config" / "gcal" / "token.json"),
        help="Path to cached token.json (default: ~/.config/gcal/token.json)",
    )
    parser.add_argument(
        "--timezone",
        default="Europe/Berlin",
        help="IANA timezone (default: Europe/Berlin)",
    )

    args = parser.parse_args()

    # Validate date
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        parser.error(f"Invalid date format: {args.date} (expected YYYY-MM-DD)")

    # Validate time
    try:
        datetime.strptime(args.start_time, "%H:%M")
    except ValueError:
        parser.error(f"Invalid time format: {args.start_time} (expected HH:MM)")

    # Validate duration
    if args.duration <= 0:
        parser.error(f"Duration must be positive: {args.duration}")

    # Validate credentials file
    if not Path(args.credentials).exists():
        parser.error(
            f"Credentials file not found: {args.credentials}\n"
            "Download OAuth2 credentials from Google Cloud Console and place them there."
        )

    return args


def authenticate(credentials_path: str, token_path: str) -> Credentials:
    """Authenticate via OAuth2 with token caching and refresh."""
    creds = None
    token_file = Path(token_path)

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
        creds = flow.run_local_server(port=0)

    # Save token for next run
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())

    return creds


def build_service(creds: Credentials):
    """Build the Google Calendar API service."""
    return build("calendar", "v3", credentials=creds)


def fetch_day_events(service, calendar_id: str, date_str: str, timezone: str) -> list[dict]:
    """Fetch all events for a given day from a single calendar."""
    day_start = f"{date_str}T00:00:00"
    day_end = f"{date_str}T23:59:59"

    result = service.events().list(
        calendarId=calendar_id,
        timeMin=f"{day_start}+00:00",
        timeMax=f"{day_end}+00:00",
        timeZone=timezone,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return result.get("items", [])


def fetch_all_day_events(service, date_str: str, timezone: str) -> list[dict]:
    """Fetch events for a given day across all user calendars."""
    calendars = service.calendarList().list().execute().get("items", [])
    # Skip holiday/week-number calendars
    skip_suffixes = ("#holiday@group.v.calendar.google.com", "#weeknum@group.v.calendar.google.com")

    all_events = []
    for cal in calendars:
        cal_id = cal["id"]
        if any(cal_id.endswith(s) for s in skip_suffixes):
            continue
        cal_name = cal.get("summary", cal_id)
        for event in fetch_day_events(service, cal_id, date_str, timezone):
            event["_calendar_name"] = cal_name
            all_events.append(event)

    # Sort: all-day events first, then by start time
    def sort_key(e):
        start = e.get("start", {})
        if "date" in start:
            return (0, "")
        return (1, start.get("dateTime", ""))

    all_events.sort(key=sort_key)
    return all_events


def format_event_time(event: dict) -> str:
    """Format an event's time range as 'HH:MM - HH:MM' or 'All day'."""
    start = event.get("start", {})
    end = event.get("end", {})

    if "date" in start:
        return "All day"

    start_dt = start.get("dateTime", "")
    end_dt = end.get("dateTime", "")

    try:
        s = datetime.fromisoformat(start_dt).strftime("%H:%M")
        e = datetime.fromisoformat(end_dt).strftime("%H:%M")
        return f"{s} - {e}"
    except (ValueError, TypeError):
        return "Unknown time"


def display_day_events(events: list[dict], date_str: str) -> None:
    """Print formatted day view of existing events."""
    print(f"\nExisting events for {date_str}:")
    print("-" * 50)

    if not events:
        print("  (no events)")
    else:
        for event in events:
            time_str = format_event_time(event)
            summary = event.get("summary", "(no title)")
            cal_name = event.get("_calendar_name", "")
            cal_suffix = f"  [{cal_name}]" if cal_name else ""
            print(f"  {time_str:17s} {summary}{cal_suffix}")

    print("-" * 50)


def display_new_event_summary(
    title: str,
    date_str: str,
    start_time: str,
    duration: int,
    timezone: str,
    description: str,
) -> None:
    """Print the planned new event details."""
    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=duration)

    print(f"\nNew event to create:")
    print(f"  Title:       {title}")
    print(f"  Date:        {date_str}")
    print(f"  Time:        {start_time} - {end_dt.strftime('%H:%M')}")
    print(f"  Duration:    {duration} min")
    print(f"  Timezone:    {timezone}")
    if description:
        print(f"  Description: {description}")
    print()


def confirm_creation() -> bool:
    """Prompt user for explicit confirmation. Returns True only on 'confirm'."""
    response = input("Type 'confirm' to create this event, or anything else to cancel: ")
    return response.strip().lower() == "confirm"


def create_event(
    service,
    calendar_id: str,
    title: str,
    date_str: str,
    start_time: str,
    duration: int,
    timezone: str,
    description: str,
) -> dict:
    """Create a calendar event and return the created resource."""
    start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + timedelta(minutes=duration)

    body = {
        "summary": title,
        "start": {
            "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": timezone,
        },
        "end": {
            "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timeZone": timezone,
        },
    }

    if description:
        body["description"] = description

    return service.events().insert(calendarId=calendar_id, body=body).execute()


def main() -> int:
    args = parse_args()

    print("Authenticating...")
    creds = authenticate(args.credentials, args.token)
    service = build_service(creds)

    events = fetch_all_day_events(service, args.date, args.timezone)
    display_day_events(events, args.date)
    display_new_event_summary(
        args.title, args.date, args.start_time, args.duration,
        args.timezone, args.description,
    )

    if not confirm_creation():
        print("Cancelled.")
        return 0

    print("Creating event...")
    event = create_event(
        service, args.calendar_id, args.title, args.date,
        args.start_time, args.duration, args.timezone, args.description,
    )

    print(f"Event created: {event.get('summary')}")
    link = event.get("htmlLink")
    if link:
        print(f"Link: {link}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
