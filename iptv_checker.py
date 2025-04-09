import re
import requests
import subprocess
import sys
import platform
import os
import gzip
import xml.etree.ElementTree as ET
import json
import shutil # <--- Добавили для перемещения файлов (обновление)
from datetime import datetime, timezone
from dateutil.parser import parse as parse_datetime
from typing import List, Dict, Optional, Tuple, Any
from packaging import version as packaging_version # <--- Для сравнения версий

# --- Rich Console ---
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import track
from rich.text import Text

console = Console()

# --- Версия программы и URL для проверки обновлений ---
CURRENT_VERSION = "1.0"
# !!! ВАЖНО: Замени на СВОЙ реальный URL к файлу version.json !!!
VERSION_URL = "YOUR_JSON_METADATA_URL_HERE" # Например, "https://raw.githubusercontent.com/user/repo/main/version.json"

# --- Константы ---
M3U_FILE = "channels.m3u"
JSON_CACHE_FILE = "channels.json"

# --- Структуры данных ---
ChannelInfo = Dict[str, Any]
EPGData = Dict[str, List[Tuple[datetime, datetime, str]]]

# --- Функция очистки консоли ---
def clear_console():
    command = 'cls' if platform.system() == "Windows" else 'clear'
    os.system(command)

# --- Функции для работы с JSON кешем каналов (без изменений) ---
def load_channels_from_json(filepath: str = JSON_CACHE_FILE) -> Optional[List[ChannelInfo]]:
    if not os.path.exists(filepath): return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            channels = json.load(f)
            if isinstance(channels, list) and all(isinstance(ch, dict) for ch in channels):
                 # console.print(f"[dim]Каналы загружены из '{filepath}'.[/dim]")
                 for i, ch in enumerate(channels): ch.setdefault('number', i + 1)
                 return channels
            else: return None
    except Exception: return None

def save_channels_to_json(channels: List[ChannelInfo], filepath: str = JSON_CACHE_FILE):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(channels, f, ensure_ascii=False, indent=2)
        # console.print(f"[dim]Список каналов сохранен в '{filepath}'.[/dim]")
    except Exception as e:
        console.print(f"[red]Ошибка сохранения JSON '{filepath}':[/red] {e}")

# --- Функция парсинга M3U (без изменений) ---
def parse_m3u(filepath: str = M3U_FILE) -> Tuple[Optional[str], List[ChannelInfo]]:
    channels: List[ChannelInfo] = []
    current_channel_info: ChannelInfo = {}
    epg_url: Optional[str] = None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line: continue
                if line.startswith('#EXTM3U'):
                    match = re.search(r'url-tvg="([^"]*)"', line)
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

# --- Функции EPG, проверки доступности, запуска плеера (без изменений) ---
def download_and_parse_epg(url: Optional[str]) -> EPGData:
    # ... (код функции без изменений) ...
    if not url: return {}
    epg_data: EPGData = {}
    timeout = 60
    try:
        headers = {'User-Agent': 'IPTV Checker Script'}
        response = requests.get(url, stream=True, timeout=timeout, headers=headers)
        response.raise_for_status()
        # console.print(f"  [dim]EPG: Заголовки получены. Скачивание/распаковка...[/dim]")
        decompressed_data = bytearray()
        chunk_size=8192; processed_chunks=0; gzip_stream=gzip.GzipFile(fileobj=response.raw)
        while True:
            try: chunk = gzip_stream.read(chunk_size);
            except EOFError: break
            except gzip.BadGzipFile: console.print(f"[bold red]Ошибка: неверный gzip EPG.[/bold red]"); return {}
            except Exception as read_err: console.print(f"[bold red]Ошибка чтения EPG: {read_err}[/bold red]"); return {}
            if not chunk: break
            decompressed_data.extend(chunk); processed_chunks += 1
            # if processed_chunks % 500 == 0: console.print(f"    [dim]~{processed_chunks * chunk_size // 1024 // 1024} MB EPG...[/dim]")
        # console.print(f"  [dim]EPG: Распаковано ~{len(decompressed_data) // 1024 // 1024} MB. Парсинг XML...[/dim]")
        if not decompressed_data: return {}
        try:
            root = ET.fromstring(decompressed_data); program_count = 0; programs_list = root.findall('programme'); total_programs = len(programs_list)
            for programme in programs_list:
                channel_id = programme.get('channel'); start_str = programme.get('start'); stop_str = programme.get('stop'); title_elem = programme.find('title')
                if channel_id and start_str and stop_str and title_elem is not None and title_elem.text:
                    try:
                        start_time = parse_datetime(start_str); stop_time = parse_datetime(stop_str); title = title_elem.text.strip()
                        if channel_id not in epg_data: epg_data[channel_id] = []
                        epg_data[channel_id].append((start_time, stop_time, title)); program_count += 1
                    except Exception: pass
            # console.print(f"  [dim]EPG: Обработано {program_count} записей.[/dim]")
        except ET.ParseError as e: console.print(f"[bold red]Ошибка парсинга XML EPG.[/bold red]"); return {}
        for channel_id in epg_data: epg_data[channel_id].sort(key=lambda x: x[0])
        # console.print(f"[green]EPG обработано.[/green]")
        return epg_data
    except requests.exceptions.Timeout: console.print(f"[bold red]Ошибка загрузки EPG: Таймаут.[/bold red]"); return {}
    except requests.exceptions.RequestException as e: console.print(f"[bold red]Ошибка загрузки EPG: {e}[/bold red]"); return {}
    except Exception as e: console.print(f"[bold red]Ошибка при обработке EPG: {e}[/bold red]"); return {}

def find_current_program(channel_id: Optional[str], epg_data: EPGData) -> Optional[str]:
    # ... (код функции без изменений) ...
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

def check_channel_availability(url: Optional[str], timeout: int = 5) -> Tuple[str, Optional[int]]:
    # ... (код функции без изменений) ...
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
    except Exception as e: status_text, status_style = f"🆘 Неизвестно ({e})", "grey50"
    return f"[{status_style}]{status_text}[/{status_style}]", status_code

def open_in_player(url: Optional[str]):
    # ... (код функции без изменений) ...
    if not url: console.print("[red]Ошибка: URL для открытия отсутствует.[/red]"); return
    player_found = False; system = platform.system(); commands = []
    vlc_path_x86 = r'C:\Program Files (x86)\VideoLAN\VLC\vlc.exe'; vlc_path_x64 = r'C:\Program Files\VideoLAN\VLC\vlc.exe'
    if system == "Windows": commands = [['vlc', url], [vlc_path_x86, url], [vlc_path_x64, url]]
    elif system == "Darwin": commands = [['open', url], ['open', '-a', 'VLC', url]]
    elif system == "Linux": commands = [['xdg-open', url], ['vlc', url]]
    else: console.print(f"[yellow]Неизвестная ОС ({system}).[/yellow]"); return
    console.print(f"\nПытаюсь открыть URL: [cyan]{url}[/cyan]")
    for cmd in commands:
        try:
            is_vlc_path = cmd[0].lower() in [vlc_path_x86.lower(), vlc_path_x64.lower()]
            if is_vlc_path and not os.path.exists(cmd[0]): continue
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try: process.wait(timeout=0.7)
            except subprocess.TimeoutExpired: pass
            else:
                if process.returncode != 0: continue
            player_found = True; console.print("  [green]Команда запущена![/green]"); break
        except FileNotFoundError: continue
        except OSError as e: console.print(f"  [red]Системная ошибка: {e}[/red]"); continue
        except Exception as e: console.print(f"  [red]Не удалось выполнить: {e}[/red]"); continue
    if not player_found:
        console.print("[bold red]\nНе удалось найти/запустить плеер.[/bold red]")
        if system == "Windows": console.print(f"Проверь пути VLC или добавь в PATH.")
        else: console.print("Убедись, что плеер (VLC) установлен.")

def display_channels_table(channels: List[ChannelInfo], epg: EPGData, statuses: Dict[int, str], filter_group: Optional[str] = None, search_term: Optional[str] = None) -> Optional[Dict[int, int]]:
    # ... (код функции без изменений) ...
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
    """Проверяет наличие обновлений."""
    if not version_url or version_url == "YOUR_JSON_METADATA_URL_HERE":
        # console.print("[dim]URL для проверки обновлений не задан.[/dim]")
        return None # Не проверяем, если URL не настроен

    console.print(f"[INFO] Проверка обновлений с [cyan]{version_url}[/cyan]...")
    try:
        headers = {'User-Agent': 'IPTV Checker Update Client'}
        response = requests.get(version_url, timeout=10, headers=headers)
        response.raise_for_status()
        data = response.json()

        latest_version_str = data.get("version")
        download_url = data.get("url")
        changelog = data.get("changelog", "Нет информации об изменениях.")

        if not latest_version_str or not download_url:
            console.print("[yellow]Ошибка:[/yellow] Неверный формат файла version.json (отсутствует 'version' или 'url').")
            return None

        # Используем packaging.version для корректного сравнения версий
        current = packaging_version.parse(current_ver)
        latest = packaging_version.parse(latest_version_str)

        if latest > current:
            console.print(f"[bold green]Доступна новая версия: {latest_version_str}[/bold green] (Текущая: {current_ver})")
            console.print(f"[bold]Изменения:[/bold] {changelog}")
            return latest_version_str, download_url, changelog
        else:
            console.print("[green]У вас последняя версия программы.[/green]")
            return None

    except requests.exceptions.RequestException as e:
        console.print(f"[yellow]Не удалось проверить обновления (ошибка сети):[/yellow] {e}")
        return None
    except json.JSONDecodeError:
        console.print(f"[yellow]Не удалось проверить обновления (ошибка чтения JSON).[/yellow]")
        return None
    except packaging_version.InvalidVersion:
         console.print(f"[yellow]Не удалось сравнить версии (неверный формат версии).[/yellow]")
         return None
    except Exception as e:
        console.print(f"[yellow]Не удалось проверить обновления (неизвестная ошибка):[/yellow] {e}")
        return None

def download_and_apply_update(download_url: str) -> bool:
    """Скачивает новую версию скрипта и заменяет текущий."""
    console.print(f"Скачивание обновления с [cyan]{download_url}[/cyan]...")
    try:
        headers = {'User-Agent': 'IPTV Checker Update Client'}
        response = requests.get(download_url, stream=True, timeout=60, headers=headers)
        response.raise_for_status()

        # Определяем пути
        current_script_path = os.path.abspath(sys.argv[0])
        script_dir = os.path.dirname(current_script_path)
        script_name = os.path.basename(current_script_path)
        new_script_path = os.path.join(script_dir, f"{script_name}.new")
        old_script_path = os.path.join(script_dir, f"{script_name}.old")

        console.print(f"Сохранение новой версии в: [dim]{new_script_path}[/dim]")
        with open(new_script_path, 'wb') as f:
            total_downloaded = 0
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                total_downloaded += len(chunk)
                # Можно добавить прогресс скачивания здесь, если нужно

        console.print(f"Скачано {total_downloaded // 1024} KB. Попытка замены файла...")

        # Удаляем старый бэкап, если он есть
        if os.path.exists(old_script_path):
            try:
                os.remove(old_script_path)
                console.print(f"  [dim]Удален старый бэкап: {old_script_path}[/dim]")
            except Exception as e_rem:
                 console.print(f"  [yellow]Не удалось удалить старый бэкап {old_script_path}: {e_rem}[/yellow]")


        # Переименовываем текущий скрипт в .old
        try:
             shutil.move(current_script_path, old_script_path)
             # os.rename(current_script_path, old_script_path) # os.rename может не работать между дисками
             console.print(f"  [dim]Текущий скрипт переименован в: {old_script_path}[/dim]")
        except Exception as e_ren_old:
            console.print(f"  [bold red]Ошибка:[/bold red] Не удалось переименовать текущий скрипт в {old_script_path}: {e_ren_old}")
            # Попытка восстановить .new, если он был скачан
            if os.path.exists(new_script_path): os.remove(new_script_path)
            return False

        # Переименовываем новый скрипт в основной
        try:
            shutil.move(new_script_path, current_script_path)
            # os.rename(new_script_path, current_script_path)
            console.print(f"  [dim]Новый скрипт перемещен на: {current_script_path}[/dim]")
            return True # Успех!
        except Exception as e_ren_new:
            console.print(f"  [bold red]КРИТИЧЕСКАЯ ОШИБКА:[/bold red] Не удалось переименовать {new_script_path} в {current_script_path}: {e_ren_new}")
            console.print(f"  [bold yellow]Попытка восстановления старой версии из {old_script_path}...[/bold yellow]")
            try:
                shutil.move(old_script_path, current_script_path)
                console.print("  [green]Старая версия восстановлена.[/green]")
            except Exception as e_recover:
                 console.print(f"  [bold red]НЕ УДАЛОСЬ ВОССТАНОВИТЬ старую версию: {e_recover}[/bold red]")
                 console.print(f"  [bold red]Программа может быть повреждена! Файлы: {old_script_path} (старый), {new_script_path} (новый)[/bold red]")
            return False


    except requests.exceptions.RequestException as e:
        console.print(f"[red]Ошибка скачивания обновления:[/red] {e}")
        return False
    except IOError as e:
        console.print(f"[red]Ошибка записи файла обновления:[/red] {e}")
        # Попытка удалить .new файл, если он остался
        new_script_path_err = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), f"{os.path.basename(sys.argv[0])}.new")
        if os.path.exists(new_script_path_err):
            try: os.remove(new_script_path_err)
            except: pass
        return False
    except Exception as e:
        console.print(f"[red]Неожиданная ошибка при обновлении:[/red] {e}")
        return False

# --- Основная часть скрипта (Главное меню) ---
if __name__ == "__main__":
    update_info = None # Хранит результат проверки обновлений

    # --- Действия при запуске ---
    clear_console()
    console.print(Panel(f"📺 IPTV Checker & Launcher v{CURRENT_VERSION} 📺",
                        style="bold blue",
                        title_align="center",
                        subtitle="Автор: t.me/jeliktontech"))

    # Проверка обновлений
    update_info = check_for_updates(CURRENT_VERSION, VERSION_URL)
    if update_info:
        latest_v, download_url, _ = update_info
        update_choice = console.input(f"Обновить до версии {latest_v}? (y/n): ").strip().lower()
        if update_choice == 'y':
            success = download_and_apply_update(download_url)
            if success:
                console.print("[bold green]\nПрограмма успешно обновлена! Пожалуйста, перезапустите скрипт.[/bold green]")
                sys.exit(0) # Выходим после успешного обновления
            else:
                console.print("[bold red]\nНе удалось применить обновление. Продолжение работы со старой версией.[/bold red]")
        else:
            console.print("Обновление отменено.")

    # --- Загрузка каналов: JSON или M3U ---
    channel_list: Optional[List[ChannelInfo]] = load_channels_from_json()
    epg_url_from_m3u = None
    if channel_list is None:
        console.print(f"\n[INFO] Загрузка из [cyan]{M3U_FILE}[/cyan]...")
        epg_url_from_m3u, channel_list = parse_m3u(M3U_FILE)
        if channel_list: save_channels_to_json(channel_list)
        else: console.print("[bold red]Не удалось загрузить каналы. Выход.[/bold red]"); sys.exit(1)
    # --- Конец загрузки каналов ---

    console.print(f"[INFO] Загружено каналов: {len(channel_list)}")

    # --- Загрузка EPG ---
    epg_url_to_use = epg_url_from_m3u # TODO: Загружать из JSON, если есть
    epg_data: EPGData = download_and_parse_epg(epg_url_to_use)

    # --- Проверка доступности ---
    console.print("\n[INFO] Проверка доступности каналов...")
    channel_statuses: Dict[int, str] = {}
    for i in track(range(len(channel_list)), description="Проверка..."):
        channel = channel_list[i]; index = channel.get('number', i + 1); url = channel.get('url')
        status_text, _ = check_channel_availability(url); channel_statuses[index] = status_text
    console.print("[green]Проверка завершена.[/green]")

    # --- Главный цикл меню ---
    # (Остальной код меню остается тем же, что и в предыдущем варианте)
    current_filter_group = None; current_search_term = None; last_displayed_map = None
    while True:
        console.print("\n" + "="*30 + " Меню " + "="*30)
        # ... (пункты меню 1-5 и q) ...
        console.print("[1] Показать список каналов")
        console.print("[2] Фильтр по группе")
        console.print("[3] Поиск по названию")
        console.print("[4] Запустить канал по номеру (из текущего списка)")
        console.print("[5] Обновить URL канала (по оригинальному номеру)")
        # Добавим пункт для повторной проверки обновлений
        console.print("[u] Проверить обновления еще раз")
        console.print("[q] Выход")
        console.print("-" * 66)

        choice = console.input("[bold cyan]Выберите действие:[/bold cyan] ").strip().lower()
        # Очистка консоли теперь в начале цикла или после вывода результата
        # clear_console() # Можно перенести сюда, если нужно чистить *до* вывода меню

        # Заголовок можно выводить здесь, если clear_console() выше
        # console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", ... subtitle="..."))

        if choice == '1':
             clear_console() # Очищаем перед выводом таблицы
             console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", style="bold blue", subtitle="Автор: t.me/jeliktontech"))
             last_displayed_map = display_channels_table(channel_list, epg_data, channel_statuses, filter_group=current_filter_group, search_term=current_search_term)
        elif choice == '2':
             clear_console()
             console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", style="bold blue", subtitle="Автор: t.me/jeliktontech"))
             # ... (логика фильтра по группе) ...
             groups = sorted(list(set(ch.get('group', 'Без группы') for ch in channel_list)))
             console.print("Доступные группы:")
             for idx, grp in enumerate(groups): console.print(f"  [{idx+1}] {grp}")
             try:
                group_choice_idx = int(console.input("Введите номер группы (или 0 для сброса): "))
                if group_choice_idx == 0: current_filter_group = None; console.print("[INFO] Фильтр по группе сброшен.")
                elif 1 <= group_choice_idx <= len(groups): current_filter_group = groups[group_choice_idx - 1]
                else: console.print("[yellow]Неверный номер группы.[/yellow]"); continue
                last_displayed_map = display_channels_table(channel_list, epg_data, channel_statuses, filter_group=current_filter_group, search_term=current_search_term)
             except ValueError: console.print("[yellow]Неверный ввод.[/yellow]")

        elif choice == '3':
             clear_console()
             console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", style="bold blue", subtitle="Автор: t.me/jeliktontech"))
             # ... (логика поиска) ...
             search_input = console.input("Введите часть названия (пусто для сброса): ").strip()
             if not search_input: current_search_term = None; console.print("[INFO] Поиск сброшен.")
             else: current_search_term = search_input
             last_displayed_map = display_channels_table(channel_list, epg_data, channel_statuses, filter_group=current_filter_group, search_term=current_search_term)

        elif choice == '4':
             # Очистка не нужна, т.к. запуск плеера - внешнее действие
             # ... (логика запуска) ...
             if last_displayed_map is None: console.print("[yellow]Сначала отобразите список (команда 1).[/yellow]"); continue
             try:
                num_input = console.input("Введите номер из ТЕКУЩЕГО списка: ")
                displayed_num = int(num_input)
                original_list_index = last_displayed_map.get(displayed_num)
                if original_list_index is not None and 0 <= original_list_index < len(channel_list):
                    selected_channel = channel_list[original_list_index]
                    console.print(f"Выбран канал (ориг. #{selected_channel.get('number')}): [cyan]{selected_channel.get('tvg_name') or selected_channel.get('name')}[/cyan]")
                    open_in_player(selected_channel.get('url'))
                else: console.print(f"[yellow]Неверный номер из текущего списка.[/yellow]")
             except ValueError: console.print("[yellow]Неверный ввод.[/yellow]")
             except Exception as e: console.print(f"[bold red]Ошибка при запуске: {e}[/bold red]")

        elif choice == '5':
             # Очистка не нужна, т.к. это диалог
             # ... (логика обновления URL) ...
             try:
                num_input = console.input("Введите ОРИГИНАЛЬНЫЙ номер канала для обновления URL: ")
                target_channel_num = int(num_input)
                channel_to_update = None; channel_index = -1
                for i, ch in enumerate(channel_list):
                    if ch.get('number') == target_channel_num: channel_to_update = ch; channel_index = i; break
                if channel_to_update:
                    console.print(f"Найден канал #{target_channel_num}: [cyan]{channel_to_update.get('tvg_name') or channel_to_update.get('name')}[/cyan]")
                    console.print(f"Текущий URL: [dim]{channel_to_update.get('url', 'Нет')}[/dim]")
                    new_url = console.input("Введите новый URL: ").strip()
                    if new_url:
                        channel_list[channel_index]['url'] = new_url
                        console.print(f"[green]URL для канала #{target_channel_num} обновлен.[/green]")
                        save_channels_to_json(channel_list)
                        console.print("[INFO] Перепроверка статуса...")
                        status_text, _ = check_channel_availability(new_url)
                        channel_statuses[target_channel_num] = status_text
                        console.print(f"Новый статус: {status_text}")
                    else: console.print("[yellow]Обновление отменено.[/yellow]")
                else: console.print(f"[yellow]Канал #{target_channel_num} не найден.[/yellow]")
             except ValueError: console.print("[yellow]Неверный ввод.[/yellow]")
             except Exception as e: console.print(f"[bold red]Ошибка при обновлении: {e}[/bold red]")

        elif choice == 'u': # Повторная проверка обновлений
             clear_console()
             console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", style="bold blue", subtitle="Автор: t.me/jeliktontech"))
             update_info = check_for_updates(CURRENT_VERSION, VERSION_URL)
             if update_info:
                 latest_v, download_url, _ = update_info
                 update_choice = console.input(f"Обновить до версии {latest_v}? (y/n): ").strip().lower()
                 if update_choice == 'y':
                     success = download_and_apply_update(download_url)
                     if success:
                         console.print("[bold green]\nУспешно обновлено! Перезапустите скрипт.[/bold green]")
                         sys.exit(0)
                     else: console.print("[bold red]\nНе удалось обновить.[/bold red]")
                 else: console.print("Обновление отменено.")

        elif choice == 'q':
             console.print("Выход."); break
        else:
             clear_console()
             console.print(Panel(f"📺 v{CURRENT_VERSION} 📺", style="bold blue", subtitle="Автор: t.me/jeliktontech"))
             console.print("[yellow]Неизвестная команда.[/yellow]")