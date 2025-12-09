#!/usr/bin/env python3
"""
Henter tabell fra https://www.enbleague.eu/standings
og oppdaterer Google Sheets.
"""

import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import time
import re

# ==========================================================
#  KONFIGURASJON
# ==========================================================

CONFIG = {
    "url": "https://www.enbleague.eu/standings",
    "sheet_id": "1jdy99JDWJ6XBZgt0wOqdieNdAIlzcwAtpRn7hUK18rM",
    "sheet_name": "Sheet1",  # Endre til riktig sheet-navn hvis nødvendig
    "credentials_file": "google_credentials.json",  # Service account JSON-fil
}

# ==========================================================
#  GOOGLE SHEETS KLIENT
# ==========================================================

def get_sheets_service():
    """Opprett Google Sheets API-klient med service account."""
    SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
    
    creds = Credentials.from_service_account_file(
        CONFIG["credentials_file"],
        scopes=SCOPES
    )
    
    service = build('sheets', 'v4', credentials=creds)
    return service

def update_sheet(service, values):
    """
    Oppdaterer Google Sheet med nye verdier.
    
    Args:
        service: Google Sheets API service
        values: 2D-liste med data (rader og kolonner)
    """
    try:
        # Tøm først eksisterende data (A1:I100 for å dekke alle kolonner)
        clear_range = f"{CONFIG['sheet_name']}!A1:I100"
        service.spreadsheets().values().clear(
            spreadsheetId=CONFIG["sheet_id"],
            range=clear_range
        ).execute()
        
        print(f"🗑️  Tømte eksisterende data")
        
        # Skriv ny data
        body = {
            'values': values
        }
        
        result = service.spreadsheets().values().update(
            spreadsheetId=CONFIG["sheet_id"],
            range=f"{CONFIG['sheet_name']}!A1",
            valueInputOption='RAW',
            body=body
        ).execute()
        
        print(f"✅ Oppdatert {result.get('updatedCells')} celler i Google Sheets")
        return result
        
    except HttpError as error:
        print(f"❌ Google Sheets API error: {error}")
        raise

# ==========================================================
#  WEB SCRAPING
# ==========================================================

def fetch_standings():
    """
    Henter tabelldata fra ENBL-nettsiden.
    
    Returns:
        2D-liste med tabelldata (headers + rader)
        Format: [Position, Team, L5, GP, W, L, WL, GD, Pts]
    """
    print(f"🌐 Henter data fra {CONFIG['url']} ...")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        response = requests.get(CONFIG["url"], headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"❌ Feil ved henting av nettside: {e}")
        raise
    
    soup = BeautifulSoup(response.text, 'lxml')
    
    # Finn tabellen - prøv flere selectors
    table = (
        soup.find('table', class_='standings') or
        soup.find('table', class_='table') or
        soup.find('table') or
        soup.find('div', class_='standings-table')
    )
    
    if not table:
        print("❌ Fant ingen tabell på siden")
        print("HTML preview (first 2000 chars):")
        print(soup.prettify()[:2000])
        raise ValueError("Ingen tabell funnet")
    
    # Headers - må matche: Position, Team, L5, GP, W, L, WL, GD, Pts
    expected_headers = ["Position", "Team", "L5", "GP", "W", "L", "WL", "GD", "Pts"]
    
    # Parse data rows
    rows = []
    tbody = table.find('tbody')
    if tbody:
        row_elements = tbody.find_all('tr')
    else:
        # Skip første rad hvis det er headers
        all_rows = table.find_all('tr')
        # Sjekk om første rad er header
        first_row_cells = all_rows[0].find_all(['th', 'td']) if all_rows else []
        has_header = any(cell.name == 'th' for cell in first_row_cells)
        row_elements = all_rows[1:] if has_header else all_rows
    
    position = 1
    for tr in row_elements:
        cells = tr.find_all(['td', 'th'])
        if not cells or len(cells) < 2:
            continue
        
        row_data = []
        
        # Position (første kolonne eller auto-generert)
        pos_text = cells[0].get_text(strip=True)
        if pos_text.isdigit():
            row_data.append(pos_text)
            cell_offset = 1
        else:
            row_data.append(str(position))
            cell_offset = 0
        
        # Parse resten av cellene
        for i in range(cell_offset, len(cells)):
            cell_text = cells[i].get_text(strip=True)
            row_data.append(cell_text)
        
        # Valider at vi har nok kolonner (minimum 8: pos, team, l5, gp, w, l, wl, gd, pts)
        if len(row_data) >= 8:
            rows.append(row_data)
            position += 1
    
    if not rows:
        print("❌ Ingen data-rader funnet i tabellen")
        raise ValueError("Ingen data funnet")
    
    print(f"✅ Hentet {len(rows)} lag fra tabellen")
    
    # Kombiner headers + data
    result = [expected_headers] + rows
    return result

def parse_standings_alternative(html_content):
    """
    Alternativ parser hvis standard table-parsing feiler.
    Prøver å finne data i div-strukturer eller andre elementer.
    """
    soup = BeautifulSoup(html_content, 'lxml')
    
    # Finn alle elementer som kan inneholde laginfo
    team_elements = soup.find_all(['div', 'tr', 'li'], class_=re.compile(r'team|standing|row', re.I))
    
    rows = []
    for idx, elem in enumerate(team_elements, 1):
        # Prøv å ekstrahere team-navn
        team_name = None
        team_link = elem.find('a')
        if team_link:
            team_name = team_link.get_text(strip=True)
        else:
            # Finn første tekst-element
            text = elem.get_text(strip=True)
            if text and len(text) > 2:
                team_name = text.split()[0] if text else None
        
        if team_name and len(team_name) > 2:
            # Placeholder-data
            row = [str(idx), team_name, "", "", "", "", "", "", ""]
            rows.append(row)
    
    if rows:
        headers = ["Position", "Team", "L5", "GP", "W", "L", "WL", "GD", "Pts"]
        return [headers] + rows
    
    return None

# ==========================================================
#  MAIN
# ==========================================================

def main():
    print("=" * 60)
    print("ENBL Standings → Google Sheets")
    print("=" * 60)
    
    try:
        # Hent data fra nettsiden
        try:
            standings_data = fetch_standings()
        except Exception as e:
            print(f"⚠️  Standard parsing feilet: {e}")
            print("🔄 Prøver alternativ parsing-metode...")
            
            # Hent HTML på nytt for alternativ parsing
            response = requests.get(CONFIG["url"], headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }, timeout=10)
            standings_data = parse_standings_alternative(response.text)
            
            if not standings_data:
                raise ValueError("Begge parsing-metodene feilet")
        
        # Vis preview
        print("\n📊 Data preview:")
        for i, row in enumerate(standings_data[:6]):
            # Pad kolonner for lesbarhet
            formatted = "\t".join(str(cell)[:20].ljust(20) for cell in row)
            print(f"  {formatted}")
        if len(standings_data) > 6:
            print(f"  ... og {len(standings_data) - 6} rader til")
        
        # Opprett Google Sheets klient
        print("\n🔑 Kobler til Google Sheets API ...")
        service = get_sheets_service()
        
        # Oppdater sheet
        print(f"📝 Oppdaterer sheet '{CONFIG['sheet_name']}' ...")
        update_sheet(service, standings_data)
        
        print(f"\n✅ FERDIG! Åpne sheet:")
        print(f"   https://docs.google.com/spreadsheets/d/{CONFIG['sheet_id']}")
        
    except FileNotFoundError:
        print(f"\n❌ FEIL: Fant ikke {CONFIG['credentials_file']}")
        print("📝 Opprett Service Account og last ned JSON-nøkkel:")
        print("   1. Gå til Google Cloud Console")
        print("   2. APIs & Services → Credentials")
        print("   3. Create Credentials → Service Account")
        print("   4. Last ned JSON-nøkkel som 'google_credentials.json'")
        print("   5. Del Google Sheet med service account e-post")
        
    except Exception as e:
        print(f"\n❌ FEIL: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()