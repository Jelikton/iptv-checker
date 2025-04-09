import re
import requests
import subprocess
import sys
import platform
import os
import gzip
import xml.etree.ElementTree as ET
import json
import shutil
from datetime import datetime, timezone
from dateutil.parser import parse as parse_datetime
from typing import List, Dict, Optional, Tuple, Any
# Убедись, что установил: pip install packaging
from packaging import version as packaging_version

# --- Rich Console ---
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import track
from rich.text import Text

console = Console()

# --- Версия программы и URL для проверки обновлений ---
CURRENT_VERSION = "1.0" # Текущая версия
# !!! ОСТАВЬ ЭТО ТАК, пока не настроишь GitHub для обновлений !!!
VERSION_URL = "YOUR_JSON_METADATA_URL_HERE"

# --- Константы ---
M3U_FILE = "channels.m3u"
JSON_CACHE_FILE = "channels.json"
EPG_PROCESSING_TIMEOUT_SECONDS = 30 # Таймаут на СКАЧИВАНИЕ EPG
MAX_EPG_XML_SIZE_MB = 75 # Макс. размер XML для ПАРСИНГА (в MB)

# --- Структуры данных ---
ChannelInfo = Dict[str, Any]
EPGData = Dict[str, List[Tuple[datetime, datetime, str]]]

# --- Функция очистки консоли ---
def clear_console():
    command = 'cls' if platform.system() == "Windows" else 'clear'
    os.system(command)

# --- Функции для работы с JSON кешем каналов ---
def load_channels_from_json(filepath: str = JSON_CACHE_FILE) -> Optional[List[ChannelInfo]]:
    if not os.path.exists(filepath): return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f: channels = json.load(f)
        if isinstance(channels, list) and all(isinstance(ch, dict) for ch in channels):
             for i, ch in enumerate(channels): ch.setdefault('number', i + 1)
             return channels
        else: return None
    except Exception: return None

def save_channels_to_json(channels: List[ChannelInfo], filepath: str = JSON_CACHE_FILE):
    try:
        with open(filepath, 'w', encoding='utf-8') as f: json.dump(channels, f, ensure_ascii=False, indent=2)
    except Exception as e: console.print(f"[red]Ошибка сохранения JSON '{filepath}':[/red] {e}")

# --- Функция парсинга M3U ---
def parse_m3u(filepath: str = M3U_FILE) -> Tuple[Optional[str], List[ChannelInfo]]:
    channels: List[ChannelInfo] = []; current_channel_info: ChannelInfo = {}; epg_url: Optional[str] = None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip();
                if not line: continue
                if line.startswith('#EXTM3U'):
                    match = re.search(r'url-tvg="([^"]*)"', line);
                    if match: epg_url = match.group(1)
                    continue
                elif line.startswith('#EXTINF:'):
                    parts = line.split(',', 1); info_part = parts[0]; name_part = parts[1] if len(parts) > 1 else "Без имени"
                    current_channel_info = {'name': name_part.strip()}
                    match = re.search(r'tvg-name="([^"]*)"', info_part); current_channel_info['tvg_name'] = match.group(1) if match else current_channel_info['name']
                    match = re.search(r'tvg-logo="([^"]*)"', info_part); current_channel_info['logo'] = match.group(1) if match else None
                    match = re.search(r'group-title="([^"]*)"', info_part); current_channel_info['group'] = match.group(1) if match else "Без группы"
                    match = re.search(r'tvg-id="([^"]*)"', info_part); current_channel_info['id'] = match.group(1) if match else None
                    current_channel_info['_waiting_for_url'] = True
                elif not line.startswith('#') and current_channel_info.get('_waiting_for_url'):
                    current_channel_info['url'] = line; del current_channel_info['_waiting_for_url']
                    current_channel_info['number'] = len(channels) + 1
                    channels.append(current_channel_info); current_channel_info = {}
    except FileNotFoundError: console.print(f"[bold red]Ошибка:[/bold red] Не найден файл '{filepath}'."); return None, []
    except Exception as e: console.print(f"[bold red]Ошибка чтения M3U '{filepath}':[/bold red] {e}"); return None, []
    return epg_url, channels

# --- Функция загрузки и парсинга EPG (с лимитом размера XML) ---
def download_and_parse_epg(url: Optional[str]) -> EPGData:
    """Скачивает, распаковывает и парсит EPG, пропуская при ошибках, таймауте или слишком большом размере XML."""
    if not url:
        return {}

    console.print(f"Попытка загрузки EPG (макс. {EPG_PROCESSING_TIMEOUT_SECONDS} сек)...", end="")
    epg_data: EPGData = {}
    try:
        headers = {'User-Agent': 'IPTV Checker Script'}
        response = requests.get(url, stream=True, timeout=EPG_PROCESSING_TIMEOUT_SECONDS, headers=headers)
        response.raise_for_status()
        console.print(" Загрузка...")

        decompressed_data = bytearray()
        gzip_stream = gzip.GzipFile(fileobj=response.raw)
        while True:
             try:
                 chunk = gzip_stream.read(8192)
                 if not chunk: break
                 decompressed_data.extend(chunk)
             except EOFError: break
             except gzip.BadGzipFile: console.print(f"\n[bold red]Ошибка: неверный gzip EPG. Пропущено.[/bold red]"); return {}
             except Exception as read_err: console.print(f"\n[bold red]Ошибка чтения EPG: {read_err}. Пропущено.[/bold red]"); return {}

        xml_size_mb = len(decompressed_data) / (1024 * 1024)
        console.print(f"  [dim]Размер XML: {xml_size_mb:.2f} MB.[/dim]")

        # --- ПРОВЕРКА РАЗМЕРА ПЕРЕД ПАРСИНГОМ ---
        if xml_size_mb > MAX_EPG_XML_SIZE_MB:
            console.print(f"[bold yellow]Предупреждение:[/bold yellow] Размер EPG XML ({xml_size_mb:.1f}MB) > лимита ({MAX_EPG_XML_SIZE_MB}MB). Парсинг пропущен.")
            return {}

        # --- Если размер в норме, парсим ---
        console.print("  Парсинг XML...")
        if not decompressed_data: return {}

        root = ET.fromstring(decompressed_data)
        program_count = 0
        programs_list = root.findall('programme')
        for programme in programs_list:
            channel_id = programme.get('channel'); start_str = programme.get('start'); stop_str = programme.get('stop'); title_elem = programme.find('title')
            if channel_id and start_str and stop_str and title_elem is not None and title_elem.text:
                try:
                    start_time = parse_datetime(start_str); stop_time = parse_datetime(stop_str); title = title_elem.text.strip()
                    if channel_id not in epg_data: epg_data[channel_id] = []
                    epg_data[channel_id].append((start_time, stop_time, title)); program_count += 1
                except Exception: pass

        for channel_id in epg_data: epg_data[channel_id].sort(key=lambda x: x[0])
        console.print(f"[green] EPG загружено ({len(epg_data)} каналов, {program_count} программ).[/green]")
        return epg_data

    except requests.exceptions.Timeout: console.print(f" [bold yellow]Таймаут! ({EPG_PROCESSING_TIMEOUT_SECONDS} сек). EPG пропущено.[/bold yellow]"); return {}
    except requests.exceptions.RequestException as e: console.print(f" [bold red]Ошибка сети EPG! ({e}) Пропущено.[/bold red]"); return {}
    except ET.ParseError as e: console.print(f" [bold red]Ошибка парсинга XML EPG! ({e}) Пропущено.[/bold red]"); return {}
    except Exception as e: console.print(f" [bold red]Неизвестная ошибка EPG! ({e}) Пропущено.[/bold red]"); return {}

# --- Функция поиска текущей программы ---
def find_current_program(channel_id: Optional[str], epg_data: EPGData) -> Optional[str]:
    if not channel_id or channel_id not in epg_data: return None
    now = datetime.now(timezone.utc)
    programs = epg_data[channel_id]
    for start, stop, title in programs:
        try:
            start_aware = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
            stop_aware = stop.astimezone(timezone.utc) if stop.tzinfo else stop.replace(tzinfo=timezone.utc)
            if start_aware <= now < stop_aware: return title
        except Exception: continue
    return None

# --- Функция проверки доступности ---
def check_channel_availability(url: Optional[str], timeout: int = 5) -> Tuple[str, Optional[int]]:
    if not url or not url.lower().startswith(('http://', 'https://')): return "[grey50]⚪ Не HTTP(S)[/grey50]", None
    headers = {'User-Agent': 'IPTV Checker Script'}; status_style, status_text, status_code = "white", "", None
    try:
        if url.lower().endswith('.mpd'): status_text, status_code, status_style = "⚪ DASH (.mpd)", None, "cyan"
        else:
            response = requests.head(url, timeout=timeout, headers=headers, allow_redirects=True, stream=False); status_code = response.status_code
            if 200 <= status_code < 300: status_text, status_style = "✅ OK", "green"
            elif status_code == 404: status_text, status_style = "❌ Не найден", "red"
            elif status_code == 403: status_text, status_style = "🚫 Запрещен", "yellow"
            elif status_code == 405: status_text, status_style = "🟡 Метод HEAD запрещен", "yellow"
            else: status_text, status_style = f"⚠️ Ошибка {status_code}", "yellow"
    except requests.exceptions.Timeout: status_text, status_style = "⏳ Таймаут", "orange3"
    except requests.exceptions.ConnectionError: status_text, status_style = "🔗 Ошибка соединения", "red"
    except requests.exceptions.RequestException: status_text, status_style = "❓ Ошибка запроса", "magenta"
    except Exception: status_text, status_style = f"🆘 Неизвестно", "grey50"
    return f"[{status_style}]{status_text}[/{status_style}]", status_code

# --- Функция для открытия плеера (Приоритет VLC) ---
def open_in_player(url: Optional[str]):
    if not url: console.print("[red]Ошибка: URL отсутствует.[/red]"); return
    player_found = False; system = platform.system(); commands = []
    vlc_path_x86 = r'C:\Program Files (x86)\VideoLAN\VLC\vlc.exe'; vlc_path_x64 = r'C:\Program Files\VideoLAN\VLC\vlc.exe'
    if system == "Windows": commands = [[vlc_path_x86, url], [vlc_path_x64, url], ['vlc', url]]
    elif system == "Darwin": commands = [['open', '-a', 'VLC', url], ['open', url]]
    elif system == "Linux": commands = [['vlc', url], ['xdg-open', url]]
    else: console.print(f"[yellow]Неизвестная ОС ({system}).[/yellow]"); return
    console.print(f"\nПытаюсь открыть URL в [bold]VLC[/bold]: [cyan]{url}[/cyan]")
    for cmd in commands:
        player_name = cmd[0]
        try:
            is_explicit_vlc_path = player_name.lower() in [vlc_path_x86.lower(), vlc_path_x64.lower()]
            if is_explicit_vlc_path and not os.path.exists(player_name): continue
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try: process.wait(timeout=0.7)
            except subprocess.TimeoutExpired: pass
            else:
                if process.returncode != 0: continue
            player_found = True; console.print(f"  [green]Команда для '{player_name}' запущена![/green]"); break
        except FileNotFoundError: continue
        except OSError as e: console.print(f"  [red]Системная ошибка '{player_name}': {e}[/red]"); continue
        except Exception as e: console.print(f"  [red]Ошибка '{' '.join(cmd)}': {e}[/red]"); continue
    if not player_found:
        console.print("[bold red]\nНе удалось найти или запустить VLC.[/bold red]")
        if system == "Windows": console.print(f"Проверь пути VLC или добавь в PATH.")
        else: console.print("Убедись, что VLC установлен.")

# --- Функция отображения таблицы каналов ---
def display_channels_table(channels: List[ChannelInfo], epg: EPGData, statuses: Dict[int, str], filter_group: Optional[str] = None, search_term: Optional[str] = None) -> Optional[Dict[int, int]]:
    table = Table(title="Список Каналов", show_header=True, header_style="bold magenta")
    table.add_column("№ (ориг.)", style="dim", width=5, justify="right")
    table.add_column("Название Канала", style="cyan", no_wrap=True, min_width=20)
    table.add_column("Группа", style="yellow", width=15)
    table.add_column("Статус", width=25)
    table.add_column("Сейчас в эфире", style="green", min_width=20, overflow="fold")
    count = 0; displayed_channel_indices = {}
    for i, channel in enumerate(channels):
        original_number = channel.get('number', i + 1)
        name = channel.get('tvg_name') or channel.get('name', 'Без имени'); group = channel.get('group', 'Без группы')
        status = statuses.get(original_number, "[grey50]Не проверен[/grey50]"); channel_id = channel.get('id')
        if filter_group and group != filter_group: continue
        if search_term and search_term.lower() not in name.lower(): continue
        now_playing = find_current_program(channel_id, epg) or "[dim]N/A[/dim]"
        count += 1; displayed_channel_indices[count] = i
        table.add_row(str(original_number), name, group, status, now_playing)
    if count == 0: console.print(Panel("[yellow]Каналы не найдены.[/yellow]", title="Результат")); return None
    else: console.print(table); return displayed_channel_indices

# --- Функции для обновления ---
def check_for_updates(current_ver: str, version_url: str) -> Optional[Tuple[str, str, str]]:
    if not version_url or version_url == "YOUR_JSON_METADATA_URL_HERE": return None
    console.print(f"[INFO] Проверка обновлений...", end="")
    try:
        headers = {'User-Agent': 'IPTV Checker Update Client', 'Cache-Control': 'no-cache'}
        response = requests.get(version_url, timeout=10, headers=headers)
        response.raise_for_status(); data = response.json()
        latest_version_str = data.get("version"); download_url = data.get("url")
        changelog = data.get("changelog", "N/A")
        if not latest_version_str or not download_url: console.print("[yellow] Ошибка version.json.[/yellow]"); return None
        current = packaging_version.parse(current_ver); latest = packaging_version.parse(latest_version_str)
        if latest > current:
            console.print(f" [bold green]Доступна v{latest_version_str}[/bold green]!")
            console.print(f"[bold]Изменения:[/bold] {changelog}")
            return latest_version_str, download_url, changelog
        else: console.print("[green] OK (последняя версия).[/green]"); return None
    except Exception: console.print("[yellow] Ошибка проверки.[/yellow]"); return None

def download_and_apply_update(download_url: str) -> bool:
    console.print(f"Скачивание обновления...")
    try:
        headers = {'User-Agent': 'IPTV Checker Update Client', 'Cache-Control': 'no-cache'}
        response = requests.get(download_url, stream=True, timeout=60, headers=headers)
        response.raise_for_status()
        current_script_path = os.path.abspath(sys.argv[0]); script_dir = os.path.dirname(current_script_path)
        script_name = os.path.basename(current_script_path)
        new_script_path = os.path.join(script_dir, f"{script_name}.new")
        old_script_path = os.path.join(script_dir, f"{script_name}.old")
        console.print(f"Сохранение в: [dim]{new_script_path}[/dim]")
        with open(new_script_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192): f.write(chunk)
        console.print(f"Замена файла...")
        if os.path.exists(old_script_path):
            try: os.remove(old_script_path)
            except Exception: pass
        shutil.move(current_script_path, old_script_path)
        shutil.move(new_script_path, current_script_path)
        return True
    except Exception as e:
        console.print(f"[red]Ошибка обновления: {e}[/red]")
        new_script_path_err = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), f"{os.path.basename(sys.argv[0])}.new")
        if os.path.exists(new_script_path_err):
            try: os.remove(new_script_path_err)
            except: pass
        old_script_path_err = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), f"{os.path.basename(sys.argv[0])}.old")
        current_script_path_err = os.path.abspath(sys.argv[0])
        if os.path.exists(old_script_path_err) and not os.path.exists(current_script_path_err):
            console.print("[yellow]Попытка восстановления из бэкапа...[/yellow]")
            try: shutil.move(old_script_path_err, current_script_path_err); console.print("[green]Восстановлено.[/green]")
            except Exception as e_rec: console.print(f"[red]Не удалось восстановить: {e_rec}[/red]")
        return False

# --- Основная часть скрипта (Главное меню) ---
if __name__ == "__main__":
    update_info = None
    clear_console()
    console.print(Panel(f"📺 IPTV Checker & Launcher v{CURRENT_VERSION} 📺",
                        style="bold blue", title_align="center", subtitle="Автор: t.me/jeliktontech"))
    update_info = check_for_updates(CURRENT_VERSION, VERSION_URL)
    # ... (остальной код проверки обновлений и главного меню без изменений) ...
    if update_info:
        latest_v, download_url, _ = update_info
        update_choice = console.input(f"Обновить до версии {latest_v}? (y/n): ").strip().lower()
        if update_choice == 'y':
            success = download_and_apply_update(download_url)
            if success: console.print("[bold green]\nУспешно обновлено! Перезапустите скрипт.[/bold green]"); sys.exit(0)
            else: console.print("[bold red]\nНе удалось обновить. Продолжение работы.[/bold red]")
        else: console.print("Обновление отменено.")

    channel_list: Optional[List[ChannelInfo]] = load_channels_from_json()
    epg_url_from_m3u = None
    if channel_list is None:
        console.print(f"\n[INFO] Загрузка из [cyan]{M3U_FILE}[/cyan]...")
        epg_url_from_m3u, channel_list = parse_m3u(M3U_FILE)
        if channel_list: save_channels_to_json(channel_list)
        else: console.print("[bold red]Не удалось загрузить каналы. Выход.[/bold red]"); sys.exit(1)

    console.print(f"[INFO] Загружено каналов: {len(channel_list)}")
    epg_url_to_use = epg_url_from_m3u
    epg_data: EPGData = download_and_parse_epg(epg_url_to_use)

    console.print("\n[INFO] Проверка доступности каналов...")
    channel_statuses: Dict[int, str] = {}
    for i in track(range(len(channel_list)), description="Проверка..."):
        channel = channel_list[i]; index = channel.get('number', i + 1); url = channel.get('url')
        status_text, _ = check_channel_availability(url); channel_statuses[index] = status_text
    console.print("[green]Проверка завершена.[/green]")

    current_filter_group = None; current_search_term = None; last_displayed_map = None
    while True:
        console.print("\n" + "="*30 + " Меню " + "="*30)
        console.print("[1] Список [2] Фильтр [3] Поиск [4] Запуск [5] Обновить URL [u] Обновления [q] Выход")
        console.print("-" * 70)
        choice = console.input("[bold cyan]Действие:[/bold cyan] ").strip().lower()

        if choice == 'q': console.print("Выход."); break

        clear_console()
        console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", style="bold blue", subtitle="Автор: t.me/jeliktontech"))

        if choice == '1':
            last_displayed_map = display_channels_table(channel_list, epg_data, channel_statuses, filter_group=current_filter_group, search_term=current_search_term)
        elif choice == '2':
            groups = sorted(list(set(ch.get('group', 'Без группы') for ch in channel_list)))
            console.print("Доступные группы:"); [console.print(f"  [{i+1}] {g}") for i, g in enumerate(groups)]
            try:
                idx = int(console.input("Номер группы (0 - сброс): "))
                if idx == 0: current_filter_group = None; console.print("[INFO] Фильтр сброшен.")
                elif 1 <= idx <= len(groups): current_filter_group = groups[idx - 1]
                else: console.print("[yellow]Неверный номер.[/yellow]"); continue
                last_displayed_map = display_channels_table(channel_list, epg_data, channel_statuses, filter_group=current_filter_group, search_term=current_search_term)
            except ValueError: console.print("[yellow]Неверный ввод.[/yellow]")
        elif choice == '3':
            search_input = console.input("Часть названия (пусто - сброс): ").strip()
            current_search_term = search_input if search_input else None
            last_displayed_map = display_channels_table(channel_list, epg_data, channel_statuses, filter_group=current_filter_group, search_term=current_search_term)
        elif choice == '4':
            if last_displayed_map is None: console.print("[yellow]Сначала покажите список (1).[/yellow]"); continue
            try:
                disp_num = int(console.input("Номер из ТЕКУЩЕГО списка: "))
                orig_idx = last_displayed_map.get(disp_num)
                if orig_idx is not None and 0 <= orig_idx < len(channel_list):
                    ch = channel_list[orig_idx]
                    console.print(f"Запуск (ориг. #{ch.get('number')}): [cyan]{ch.get('tvg_name') or ch.get('name')}[/cyan]")
                    open_in_player(ch.get('url'))
                else: console.print(f"[yellow]Неверный номер.[/yellow]")
            except ValueError: console.print("[yellow]Неверный ввод.[/yellow]")
            except Exception as e: console.print(f"[bold red]Ошибка запуска: {e}[/bold red]")
        elif choice == '5':
            try:
                target_num = int(console.input("ОРИГИНАЛЬНЫЙ номер канала для обновления URL: "))
                ch_upd = None; ch_idx = -1
                for i, ch in enumerate(channel_list):
                    if ch.get('number') == target_num: ch_upd = ch; ch_idx = i; break
                if ch_upd:
                    console.print(f"Канал #{target_num}: [cyan]{ch_upd.get('tvg_name') or ch_upd.get('name')}[/cyan]")
                    console.print(f"Текущий URL: [dim]{ch_upd.get('url', 'Нет')}[/dim]")
                    new_url = console.input("Новый URL: ").strip()
                    if new_url:
                        channel_list[ch_idx]['url'] = new_url; save_channels_to_json(channel_list)
                        console.print(f"[green]URL обновлен. Перепроверка статуса...[/green]")
                        status_text, _ = check_channel_availability(new_url)
                        channel_statuses[target_num] = status_text
                        console.print(f"Новый статус: {status_text}")
                    else: console.print("[yellow]Обновление отменено.[/yellow]")
                else: console.print(f"[yellow]Канал #{target_num} не найден.[/yellow]")
            except ValueError: console.print("[yellow]Неверный ввод.[/yellow]")
            except Exception as e: console.print(f"[bold red]Ошибка обновления: {e}[/bold red]")
        elif choice == 'u':
             update_info = check_for_updates(CURRENT_VERSION, VERSION_URL)
             if update_info:
                 latest_v, download_url, _ = update_info
                 update_choice = console.input(f"Обновить до версии {latest_v}? (y/n): ").strip().lower()
                 if update_choice == 'y':
                     success = download_and_apply_update(download_url)
                     if success: console.print("[bold green]\nУспешно обновлено! Перезапустите скрипт.[/bold green]"); sys.exit(0)
                     else: console.print("[bold red]\nНе удалось обновить.[/bold red]")
                 else: console.print("Обновление отменено.")
             elif VERSION_URL != "YOUR_JSON_METADATA_URL_HERE":
                  console.input("Нажмите Enter для возврата в меню...") # Пауза

        else:
            console.print("[yellow]Неизвестная команда.[/yellow]")
            console.input("Нажмите Enter для возврата в меню...") # Пауза при неверной команде