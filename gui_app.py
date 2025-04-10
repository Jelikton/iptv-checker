import customtkinter as ctk
from customtkinter import filedialog
import platform
import os
import sys # <--- Добавили sys для определения _MEIPASS
import json
import re
import requests
import subprocess
import threading
import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dateutil.parser import parse as parse_datetime
from typing import List, Dict, Optional, Tuple, Any
# Убедись, что установил: pip install packaging
from packaging import version as packaging_version

# --- Rich Console (используется только для print) ---
from rich.console import Console
console = Console()

# --- Версия программы и URL для проверки обновлений ---
CURRENT_VERSION = "1.2-GUI"
VERSION_URL = "YOUR_JSON_METADATA_URL_HERE" # Оставь так до настройки

# --- Константы и пути ---

# Функция для определения правильного пути к ресурсам (для PyInstaller)
def resource_path(relative_path):
    """ Возвращает абсолютный путь к ресурсу, работает для dev и для PyInstaller """
    try:
        # PyInstaller создает временную папку и сохраняет путь в _MEIPASS
        base_path = sys._MEIPASS
        # print(f"[DEBUG] Running frozen, base path: {base_path}") # Для отладки
    except Exception:
        # _MEIPASS не установлен, значит работаем в обычном режиме
        base_path = os.path.dirname(os.path.abspath(__file__))
        # print(f"[DEBUG] Running as script, base path: {base_path}") # Для отладки
    return os.path.join(base_path, relative_path)

# Функция для определения пути к папке, где лежит EXE или скрипт
def get_executable_dir():
    """Возвращает путь к папке с исполняемым файлом или скриптом."""
    if getattr(sys, 'frozen', False):
        # Если запущено из PyInstaller bundle (.exe)
        return os.path.dirname(sys.executable)
    else:
        # Если запущено как обычный скрипт (.py)
        return os.path.dirname(os.path.abspath(__file__))

APP_DIR = get_executable_dir() # Папка, где лежит EXE или PY
M3U_FILE_PATH = resource_path("channels.m3u") # Путь к M3U внутри EXE или рядом с PY
JSON_CACHE_FILE_PATH = os.path.join(APP_DIR, "channels.json") # Путь к JSON рядом с EXE/PY
CONFIG_FILE_PATH = os.path.join(APP_DIR, "config.json") # Путь к конфигу рядом с EXE/PY

EPG_PROCESSING_TIMEOUT_SECONDS = 30
MAX_EPG_XML_SIZE_MB = 75
CHECK_TIMEOUT_SECONDS = 5

# --- Структуры данных ---
ChannelInfo = Dict[str, Any]
EPGData = Dict[str, List[Tuple[datetime, datetime, str]]]
ChannelStatus = Tuple[str, Optional[int], str] # (text, code, color)

# --- Вспомогательные функции ---

# --- Функции для работы с JSON (теперь используют правильные пути) ---
def load_channels_from_json(filepath: str = JSON_CACHE_FILE_PATH) -> Optional[List[ChannelInfo]]:
    if not os.path.exists(filepath): return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f: channels = json.load(f)
        if isinstance(channels, list) and all(isinstance(ch, dict) for ch in channels):
             print(f"[INFO] Загружено из '{filepath}'.")
             for i, ch in enumerate(channels): ch.setdefault('number', i + 1)
             return channels
        else: print(f"[WARNING] Файл '{filepath}' имеет неверную структуру."); return None
    except json.JSONDecodeError: print(f"[WARNING] Не удалось декодировать JSON из '{filepath}'."); return None
    except Exception as e: print(f"[ERROR] Чтение JSON кеша '{filepath}': {e}"); return None

def save_channels_to_json(channels: List[ChannelInfo], filepath: str = JSON_CACHE_FILE_PATH):
    try:
        with open(filepath, 'w', encoding='utf-8') as f: json.dump(channels, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Список каналов сохранен в '{filepath}'.")
    except Exception as e: print(f"[ERROR] Ошибка сохранения JSON '{filepath}': {e}")

def load_config(filepath: str = CONFIG_FILE_PATH) -> Dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: config = json.load(f)
            if isinstance(config, dict): return config
        except Exception as e: print(f"[WARNING] Не удалось загрузить config.json: {e}")
    return {}

def save_config(config_data: Dict, filepath: str = CONFIG_FILE_PATH):
    try:
        with open(filepath, 'w', encoding='utf-8') as f: json.dump(config_data, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Конфигурация сохранена в '{filepath}'.")
    except Exception as e: print(f"[ERROR] Ошибка сохранения config.json: {e}")

# --- Функция парсинга M3U (теперь использует resource_path) ---
def parse_m3u_simplified(filepath: str = M3U_FILE_PATH) -> tuple[str | None, list]:
    channels = []; current_channel_info = {}; epg_url = None
    try:
        # --- Используем filepath, который уже обработан resource_path ---
        print(f"[DEBUG] Trying to parse M3U from: {filepath}") # Отладочный вывод
        with open(filepath, 'r', encoding='utf-8') as f:
            for i, line in enumerate(f):
                line = line.strip();
                if not line: continue
                if line.startswith('#EXTM3U'):
                    match = re.search(r'url-tvg="([^"]*)"', line);
                    if match: epg_url = match.group(1)
                    continue
                elif line.startswith('#EXTINF:'):
                    parts = line.split(',', 1); info_part = parts[0]; name_part = parts[1] if len(parts) > 1 else f"Канал {len(channels)+1}"
                    current_channel_info = {'name': name_part.strip(), 'number': len(channels) + 1}
                    match = re.search(r'tvg-name="([^"]*)"', info_part); current_channel_info['tvg_name'] = match.group(1) if match else current_channel_info['name']
                    match = re.search(r'tvg-logo="([^"]*)"', info_part); current_channel_info['logo'] = match.group(1) if match else None
                    match = re.search(r'group-title="([^"]*)"', info_part); current_channel_info['group'] = match.group(1) if match else "Без группы"
                    match = re.search(r'tvg-id="([^"]*)"', info_part); current_channel_info['id'] = match.group(1) if match else None
                    current_channel_info['_waiting_for_url'] = True
                elif not line.startswith('#') and current_channel_info.get('_waiting_for_url'):
                    current_channel_info['url'] = line; del current_channel_info['_waiting_for_url']
                    channels.append(current_channel_info); current_channel_info = {}
    except FileNotFoundError:
         # Добавляем информацию о том, где искали файл
         print(f"ERROR: M3U file not found at expected location: '{filepath}'")
         # Попробуем найти рядом с exe на всякий случай (хотя это не должно быть нужно с resource_path)
         alt_path = os.path.join(APP_DIR, os.path.basename(filepath))
         if os.path.exists(alt_path):
             print(f"WARNING: Found M3U at alternate path: {alt_path}. Check PyInstaller packaging.")
             # Можно попробовать прочитать отсюда, но лучше исправить сборку
         return None, []
    except Exception as e: print(f"ERROR reading M3U '{filepath}': {e}"); return None, []
    return epg_url, channels

# --- Остальные вспомогательные функции (EPG, Status, Player, Table) без изменений в логике, но используют новые пути ---
def download_and_parse_epg_worker(url: Optional[str], result_dict: Dict) -> None:
    # ... (код как в предыдущем примере) ...
    if not url: result_dict['epg'] = {}; return
    epg_data: EPGData = {}
    try:
        print(f"[EPG THREAD] Starting download from {url}...")
        headers = {'User-Agent': 'IPTV Checker GUI'}
        response = requests.get(url, stream=True, timeout=EPG_PROCESSING_TIMEOUT_SECONDS, headers=headers)
        response.raise_for_status(); print(f"[EPG THREAD] Download started. Decompressing...")
        decompressed_data = bytearray(); gzip_stream = gzip.GzipFile(fileobj=response.raw)
        while True:
            try: chunk = gzip_stream.read(8192);
            except EOFError: break
            except gzip.BadGzipFile: print("[EPG THREAD ERROR] Bad Gzip file."); result_dict['epg'] = {}; return
            except Exception as e: print(f"[EPG THREAD ERROR] Read error: {e}"); result_dict['epg'] = {}; return
            if not chunk: break; decompressed_data.extend(chunk)
        xml_size_mb = len(decompressed_data) / (1024 * 1024); print(f"[EPG THREAD] Decompressed size: {xml_size_mb:.2f} MB")
        if xml_size_mb > MAX_EPG_XML_SIZE_MB: print(f"[EPG THREAD WARNING] EPG XML too large. Skipping parse."); result_dict['epg'] = {}; return
        if not decompressed_data: result_dict['epg'] = {}; return
        print(f"[EPG THREAD] Parsing XML..."); root = ET.fromstring(decompressed_data); program_count = 0; programs_list = root.findall('programme')
        print(f"[EPG THREAD] Found {len(programs_list)} programs. Processing...")
        for programme in programs_list:
            channel_id=programme.get('channel');start_str=programme.get('start');stop_str=programme.get('stop');title_elem=programme.find('title')
            if channel_id and start_str and stop_str and title_elem is not None and title_elem.text:
                try:
                    start_time=parse_datetime(start_str);stop_time=parse_datetime(stop_str);title=title_elem.text.strip()
                    if channel_id not in epg_data: epg_data[channel_id]=[]
                    epg_data[channel_id].append((start_time,stop_time,title));program_count+=1
                except Exception: pass
        for channel_id in epg_data: epg_data[channel_id].sort(key=lambda x:x[0])
        print(f"[EPG THREAD] Finished. Parsed {program_count} programs for {len(epg_data)} channels.")
        result_dict['epg'] = epg_data
    except requests.exceptions.Timeout: print(f"[EPG THREAD ERROR] Timeout"); result_dict['epg'] = {}
    except requests.exceptions.RequestException as e: print(f"[EPG THREAD ERROR] Network error: {e}"); result_dict['epg'] = {}
    except ET.ParseError as e: print(f"[EPG THREAD ERROR] XML Parse error: {e}"); result_dict['epg'] = {}
    except Exception as e: print(f"[EPG THREAD ERROR] Unknown error: {e}"); result_dict['epg'] = {}


def find_current_program(channel_id: Optional[str], epg_data: EPGData) -> Optional[str]:
    # ... (код как в предыдущем примере) ...
    if not channel_id or channel_id not in epg_data: return None
    now = datetime.now(timezone.utc); programs = epg_data.get(channel_id, [])
    for start, stop, title in programs:
        try:
            start_aware = start.astimezone(timezone.utc) if start.tzinfo else start.replace(tzinfo=timezone.utc)
            stop_aware = stop.astimezone(timezone.utc) if stop.tzinfo else stop.replace(tzinfo=timezone.utc)
            if start_aware <= now < stop_aware: return title
        except Exception: continue
    return None

def check_channel_availability_worker(channel_num: int, url: Optional[str], result_dict: Dict) -> None:
    # ... (код как в предыдущем примере) ...
    status_text = "Не HTTP(S)"; status_code = None; status_color = "gray"
    if url and url.lower().startswith(('http://', 'https://')):
        headers = {'User-Agent': 'IPTV Checker GUI'}
        try:
            if url.lower().endswith('.mpd'): status_text, status_code, status_color = "DASH (.mpd)", None, "cyan"
            else:
                response = requests.head(url, timeout=CHECK_TIMEOUT_SECONDS, headers=headers, allow_redirects=True, stream=False)
                status_code = response.status_code
                if 200 <= status_code < 300: status_text, status_color = "OK", "green"
                elif status_code == 404: status_text, status_color = "Не найден", "red"
                elif status_code == 403: status_text, status_color = "Запрещен", "orange"
                elif status_code == 405: status_text, status_color = "Метод HEAD X", "orange"
                else: status_text, status_color = f"Ошибка {status_code}", "orange"
        except requests.exceptions.Timeout: status_text, status_color = "Таймаут", "orange"
        except requests.exceptions.ConnectionError: status_text, status_color = "Нет соедин.", "red"
        except requests.exceptions.RequestException: status_text, status_color = "Ошибка зап.", "magenta"
        except Exception: status_text, status_color = "Неизвестно", "gray"
    result_dict[channel_num] = (status_text, status_code, status_color)


def open_in_player(url: Optional[str], console_print_func, custom_player_path: Optional[str] = None):
    # ... (код как в предыдущем примере) ...
    if not url: console_print_func("[red]Ошибка: URL отсутствует.[/red]"); return
    player_found = False; system = platform.system(); commands = []
    vlc_path_x86 = r'C:\Program Files (x86)\VideoLAN\VLC\vlc.exe'; vlc_path_x64 = r'C:\Program Files\VideoLAN\VLC\vlc.exe'
    if custom_player_path and os.path.exists(custom_player_path): commands.append([custom_player_path, url]); console_print_func(f"Запуск через: [cyan]{custom_player_path}[/cyan]")
    elif custom_player_path: console_print_func(f"[yellow]Указ. путь не найден:[/yellow] {custom_player_path}")
    if system == "Windows": commands.extend([[vlc_path_x86, url], [vlc_path_x64, url], ['vlc', url]])
    elif system == "Darwin": commands.extend([['open', '-a', 'VLC', url], ['open', url]])
    elif system == "Linux": commands.extend([['vlc', url], ['xdg-open', url]])
    else: console_print_func(f"[yellow]Неизвестная ОС ({system}).[/yellow]"); return
    if not commands: console_print_func("[red]Нет команд для запуска плеера.[/red]"); return # Добавлено для ясности
    for cmd in commands:
        player_name = cmd[0]
        try:
            is_explicit_path = player_name.lower() in [vlc_path_x86.lower(), vlc_path_x64.lower()] or player_name == custom_player_path
            if is_explicit_path and not os.path.exists(player_name): continue
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            player_found = True; console_print_func(f"  [green]Команда '{os.path.basename(player_name)}' запущена.[/green]"); break
        except FileNotFoundError: continue
        except Exception as e: console_print_func(f"  [red]Ошибка '{player_name}': {e}[/red]"); continue
    if not player_found: console_print_func("[bold red]\nНе удалось найти/запустить плеер.[/bold red]")


# --- Класс окна настроек (использует CONFIG_FILE_PATH) ---
class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, master):
        super().__init__(master)
        self.app = master
        self.title("Настройки"); self.geometry("500x200"); self.transient(master); self.grab_set()
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(self, text="Путь к исполняемому файлу плеера (.exe):").grid(row=0, column=0, padx=20, pady=(20, 5), columnspan=2, sticky="w")
        self.path_entry = ctk.CTkEntry(self, width=350); self.path_entry.grid(row=1, column=0, padx=(20, 5), pady=5, sticky="ew")
        self.path_entry.insert(0, self.app.config.get("player_path", ""))
        self.browse_button = ctk.CTkButton(self, text="Обзор...", width=80, command=self.browse_file); self.browse_button.grid(row=1, column=1, padx=(0, 20), pady=5)
        self.button_frame = ctk.CTkFrame(self, fg_color="transparent"); self.button_frame.grid(row=3, column=0, columnspan=2, padx=20, pady=(10, 20), sticky="e")
        self.save_button = ctk.CTkButton(self.button_frame, text="Сохранить", command=self.save_settings); self.save_button.grid(row=0, column=0, padx=5)
        self.cancel_button = ctk.CTkButton(self.button_frame, text="Отмена", fg_color="gray", command=self.destroy); self.cancel_button.grid(row=0, column=1, padx=5)
    def browse_file(self):
        filetypes = (("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")) if platform.system() == "Windows" else (("Все файлы", "*.*"),)
        filepath = filedialog.askopenfilename(title="Выберите плеер", filetypes=filetypes)
        if filepath: self.path_entry.delete(0, "end"); self.path_entry.insert(0, filepath)
    def save_settings(self):
        new_path = self.path_entry.get().strip()
        self.app.config["player_path"] = new_path
        self.app.save_app_config(); print(f"[SETTINGS] Player path set to: {new_path if new_path else 'Default'}"); self.destroy()


# --- Основной класс приложения ---
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"IPTV Checker by jeliktontech (v{CURRENT_VERSION})"); self.geometry("950x650")
        ctk.set_appearance_mode("Dark"); ctk.set_default_color_theme("dark-blue")
        self.config = load_config() # Загружаем конфиг ДО виджетов
        self.channels: List[ChannelInfo] = []; self.epg_data: EPGData = {}; self.channel_statuses: Dict[int, ChannelStatus] = {}
        self.channel_widgets: List[ctk.CTkButton] = []; self.selected_channel_widget: Optional[ctk.CTkButton] = None
        self.selected_channel_data: Optional[ChannelInfo] = None; self.epg_thread: Optional[threading.Thread] = None
        self.status_threads: List[threading.Thread] = []; self.stop_status_check: bool = False; self.settings_window: Optional[SettingsWindow] = None

        # --- Макет ---
        self.grid_columnconfigure(0, weight=1, minsize=250); self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1); self.grid_rowconfigure(1, weight=0)
        # Левая панель
        self.left_frame = ctk.CTkFrame(self, width=250); self.left_frame.grid(row=0, column=0, padx=(10, 5), pady=10, sticky="nsew")
        self.left_frame.grid_rowconfigure(1, weight=1); self.left_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.left_frame, text="Каналы", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        self.channel_list_frame = ctk.CTkScrollableFrame(self.left_frame, fg_color="transparent")
        self.channel_list_frame.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="nsew"); self.channel_list_frame.grid_columnconfigure(0, weight=1)
        # Правая панель
        self.right_frame = ctk.CTkFrame(self); self.right_frame.grid(row=0, column=1, padx=(5, 10), pady=10, sticky="nsew")
        self.right_frame.grid_columnconfigure(0, weight=1); self.right_frame.grid_rowconfigure(3, weight=1)
        self.info_title_label = ctk.CTkLabel(self.right_frame, text="Канал не выбран", font=ctk.CTkFont(size=18, weight="bold"), anchor="w"); self.info_title_label.grid(row=0, column=0, padx=20, pady=(20, 5), sticky="ew")
        self.info_group_label = ctk.CTkLabel(self.right_frame, text="Группа: -", anchor="w"); self.info_group_label.grid(row=1, column=0, padx=20, pady=2, sticky="ew")
        self.info_url_label = ctk.CTkLabel(self.right_frame, text="URL: -", anchor="w", wraplength=500); self.info_url_label.grid(row=2, column=0, padx=20, pady=2, sticky="ew")
        self.details_frame = ctk.CTkFrame(self.right_frame, fg_color="transparent"); self.details_frame.grid(row=4, column=0, padx=15, pady=10, sticky="ew"); self.details_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.details_frame, text="Сейчас в эфире:", font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=5, pady=(5, 0), sticky="w")
        self.epg_now_label = ctk.CTkLabel(self.details_frame, text="N/A", anchor="w", justify="left", wraplength=450); self.epg_now_label.grid(row=1, column=0, padx=5, pady=(0, 10), sticky="ew")
        ctk.CTkLabel(self.details_frame, text="Статус:", font=ctk.CTkFont(weight="bold")).grid(row=2, column=0, padx=5, pady=(5, 0), sticky="w")
        self.status_label_channel = ctk.CTkLabel(self.details_frame, text="N/A", anchor="w"); self.status_label_channel.grid(row=3, column=0, padx=5, pady=(0, 10), sticky="ew")
        self.button_frame = ctk.CTkFrame(self.right_frame, fg_color="transparent"); self.button_frame.grid(row=5, column=0, padx=15, pady=15, sticky="ew"); self.button_frame.grid_columnconfigure((0, 1, 2), weight=1)
        self.refresh_button = ctk.CTkButton(self.button_frame, text="Обновить статусы", command=self.refresh_statuses_threaded); self.refresh_button.grid(row=0, column=0, padx=5, pady=10, sticky="ew")
        self.settings_button = ctk.CTkButton(self.button_frame, text="Настройки", command=self.open_settings_window); self.settings_button.grid(row=0, column=1, padx=5, pady=10, sticky="ew")
        self.launch_button = ctk.CTkButton(self.button_frame, text="Запустить в VLC", command=self.launch_channel, state="disabled"); self.launch_button.grid(row=0, column=2, padx=5, pady=10, sticky="ew")
        # Нижняя панель
        self.bottom_frame = ctk.CTkFrame(self, height=50); self.bottom_frame.grid(row=1, column=0, columnspan=2, padx=10, pady=(5, 10), sticky="ew"); self.bottom_frame.grid_columnconfigure(0, weight=1)
        self.statusbar = ctk.CTkLabel(self.bottom_frame, text=" Инициализация...", anchor="w"); self.statusbar.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        self.progress_bar = ctk.CTkProgressBar(self.bottom_frame, orientation="horizontal", mode="determinate"); self.progress_bar.set(0)

        # Загрузка данных
        self.load_initial_data(); self.start_epg_load()

    # --- Методы ---
    def load_initial_data(self):
        self.update_statusbar("Загрузка списка каналов...")
        loaded_channels = load_channels_from_json() # Использует JSON_CACHE_FILE_PATH
        self.epg_url_from_m3u = None # EPG URL из M3U, если его парсили
        if loaded_channels is None:
            # --- Используем M3U_FILE_PATH, определенный через resource_path ---
            m3u_path_to_check = M3U_FILE_PATH
            if os.path.exists(m3u_path_to_check): # Проверяем существование файла по правильному пути
                self.update_statusbar(f"Чтение из {os.path.basename(m3u_path_to_check)}...")
                print(f"[INFO] Чтение из {m3u_path_to_check}...")
                self.epg_url_from_m3u, channels_from_m3u = parse_m3u_simplified(m3u_path_to_check)
                if channels_from_m3u:
                    self.update_statusbar(f"Спарсено {len(channels_from_m3u)} кан. Сохранение...")
                    loaded_channels = channels_from_m3u
                    save_channels_to_json(loaded_channels) # Сохраняет в JSON_CACHE_FILE_PATH
                else: self.update_statusbar(f"Не удалось прочитать из {os.path.basename(m3u_path_to_check)}"); loaded_channels = []
            else:
                 self.update_statusbar(f"Не найдены файлы {os.path.basename(JSON_CACHE_FILE_PATH)} и {os.path.basename(M3U_FILE_PATH)}")
                 loaded_channels = []
        self.channels = loaded_channels if loaded_channels is not None else []
        self.populate_channel_list()
        if self.channels:
            self.update_statusbar(f"Загружено каналов: {len(self.channels)}. Проверка статусов...")
            self.refresh_statuses_threaded()

    def populate_channel_list(self):
        # ... (код как в предыдущем примере) ...
        for widget in self.channel_widgets: widget.destroy()
        self.channel_widgets = []; self.selected_channel_widget = None
        self.reset_info_panel()
        for i, channel in enumerate(self.channels):
            num = channel.get('number', i + 1); name = channel.get('tvg_name') or channel.get('name', f'Канал {num}')
            channel_button = ctk.CTkButton(self.channel_list_frame, text=f"{num}. {name}", fg_color="transparent", text_color=("gray10", "gray90"), hover=False, anchor="w")
            channel_button.configure(command=lambda w=channel, btn=channel_button: self.select_channel(widget_button=btn, channel_data=w))
            channel_button.grid(row=i, column=0, padx=5, pady=(1, 1), sticky="ew"); self.channel_widgets.append(channel_button)


    def select_channel(self, widget_button: ctk.CTkButton, channel_data: ChannelInfo):
        # ... (код как в предыдущем примере) ...
        if self.selected_channel_widget and self.selected_channel_widget != widget_button: self.selected_channel_widget.configure(fg_color="transparent")
        widget_button.configure(fg_color=("gray75", "gray25")); self.selected_channel_widget = widget_button; self.selected_channel_data = channel_data
        ch_name = channel_data.get('tvg_name') or channel_data.get('name', ''); ch_group = channel_data.get('group', 'N/A')
        ch_url = channel_data.get('url'); ch_num = channel_data.get('number', -1); ch_id = channel_data.get('id')
        self.info_title_label.configure(text=f"{ch_num}. {ch_name}"); self.info_group_label.configure(text=f"Группа: {ch_group}"); self.info_url_label.configure(text=f"URL: {ch_url or 'Нет'}")
        current_program = find_current_program(ch_id, self.epg_data); self.epg_now_label.configure(text=f"{current_program or 'N/A'}")
        status_info = self.channel_statuses.get(ch_num)
        if status_info: status_text, _, status_color = status_info; self.status_label_channel.configure(text=status_text, text_color=status_color)
        else: self.status_label_channel.configure(text="Не проверен", text_color="gray")
        self.launch_button.configure(state="normal" if ch_url else "disabled"); self.update_statusbar(f"Выбран канал #{ch_num}: {ch_name}")

    def reset_info_panel(self):
        # ... (код как в предыдущем примере) ...
        self.info_title_label.configure(text="Канал не выбран"); self.info_group_label.configure(text="Группа: -"); self.info_url_label.configure(text="URL: -")
        self.epg_now_label.configure(text="N/A"); self.status_label_channel.configure(text="N/A", text_color=("gray10", "gray90")); self.launch_button.configure(state="disabled")
        if self.selected_channel_widget: self.selected_channel_widget.configure(fg_color="transparent"); self.selected_channel_widget = None
        self.selected_channel_data = None

    def update_statusbar(self, text: str):
        # ... (код как в предыдущем примере) ...
        self.statusbar.configure(text=f" {text}")

    def update_channel_status_display(self, channel_num: int, status_info: ChannelStatus):
        # ... (код как в предыдущем примере) ...
        self.channel_statuses[channel_num] = status_info
        if self.selected_channel_data and self.selected_channel_data.get('number') == channel_num:
            status_text, _, status_color = status_info
            self.status_label_channel.configure(text=status_text, text_color=status_color)

    def start_epg_load(self):
        # ... (код как в предыдущем примере, использует self.epg_url_from_m3u) ...
        if self.epg_thread and self.epg_thread.is_alive(): return
        epg_url_to_use = self.epg_url_from_m3u # Берем URL, полученный при парсинге M3U
        if epg_url_to_use:
            self.update_statusbar("Загрузка EPG в фоне...")
            self.epg_result = {}
            self.epg_thread = threading.Thread(target=download_and_parse_epg_worker, args=(epg_url_to_use, self.epg_result), daemon=True)
            self.epg_thread.start(); self.after(100, self.check_epg_result)

    def check_epg_result(self):
        # ... (код как в предыдущем примере) ...
        if self.epg_thread and not self.epg_thread.is_alive():
            self.epg_data = self.epg_result.get('epg', {}); status_msg = "EPG загружено." if self.epg_data else "Не удалось загрузить EPG."
            self.update_statusbar(status_msg + " Проверка статусов может продолжаться.")
            if self.selected_channel_data: ch_id = self.selected_channel_data.get('id'); current_program = find_current_program(ch_id, self.epg_data); self.epg_now_label.configure(text=f"{current_program or 'N/A'}")
            self.epg_thread = None
        elif self.epg_thread and self.epg_thread.is_alive(): self.after(500, self.check_epg_result)


    def refresh_statuses_threaded(self):
        # ... (код как в предыдущем примере) ...
        if any(t.is_alive() for t in self.status_threads): self.update_statusbar("Проверка статусов уже идет..."); return
        self.update_statusbar("Запуск проверки статусов..."); self.channel_statuses.clear(); self.status_threads = []; self.stop_status_check = False
        total_channels = len(self.channels); self.checked_count = 0
        if total_channels == 0: self.update_statusbar("Нет каналов для проверки."); return
        self.progress_bar.grid(row=0, column=1, padx=10, pady=5, sticky="e"); self.progress_bar.set(0)
        self.refresh_button.configure(state="disabled", text="Проверка...")
        for i, channel_data in enumerate(self.channels):
            if self.stop_status_check: break
            channel_num = channel_data.get('number', i + 1); url = channel_data.get('url')
            thread = threading.Thread(target=self.check_single_channel_and_update, args=(channel_num, url, total_channels), daemon=True)
            self.status_threads.append(thread); thread.start()
        self.after(100, self.check_status_completion)

    def check_single_channel_and_update(self, channel_num, url, total_channels):
        # ... (код как в предыдущем примере) ...
        status_info: ChannelStatus = ("Ошибка потока", None, "gray")
        try: check_result = {}; check_channel_availability_worker(channel_num, url, check_result); status_info = check_result.get(channel_num, status_info)
        finally:
            with threading.Lock(): self.checked_count += 1
            self.after(0, lambda ch_num=channel_num, status=status_info, total=total_channels: self._update_progress_and_status(ch_num, status, total))

    def _update_progress_and_status(self, channel_num, status_info, total_channels):
        # ... (код как в предыдущем примере) ...
        self.update_channel_status_display(channel_num, status_info)
        progress = self.checked_count / total_channels if total_channels > 0 else 0
        self.progress_bar.set(progress); self.update_statusbar(f"Проверка: {self.checked_count}/{total_channels}")

    def check_status_completion(self):
        # ... (код как в предыдущем примере) ...
        active_threads = [t for t in self.status_threads if t.is_alive()]
        if not active_threads:
            self.update_statusbar(f"Проверка статусов завершена.")
            self.progress_bar.grid_forget(); self.refresh_button.configure(state="normal", text="Обновить статусы")
            self.status_threads = []
        else: self.after(500, self.check_status_completion)

    def launch_channel(self):
        # ... (код как в предыдущем примере, использует self.config) ...
        if self.selected_channel_data:
            url = self.selected_channel_data.get('url'); name = self.selected_channel_data.get('tvg_name') or self.selected_channel_data.get('name')
            if url:
                self.update_statusbar(f"Запуск '{name}'...")
                player_path = self.config.get("player_path", None)
                thread = threading.Thread(target=open_in_player, args=(url, self.update_statusbar, player_path), daemon=True)
                thread.start()
            else: self.update_statusbar("Ошибка: У этого канала нет URL.")
        else: self.update_statusbar("Канал не выбран.")

    def open_settings_window(self):
        # ... (код как в предыдущем примере) ...
        if self.settings_window is None or not self.settings_window.winfo_exists():
            self.settings_window = SettingsWindow(self); self.settings_window.focus()
        else: self.settings_window.focus()

    def save_app_config(self):
        # ... (код как в предыдущем примере) ...
        save_config(self.config) # Использует CONFIG_FILE_PATH

    def on_closing(self):
        # ... (код как в предыдущем примере) ...
        print("Завершение работы...")
        self.stop_status_check = True
        self.destroy()

# --- Точка входа ---
if __name__ == "__main__":
    # Код проверки обновлений убран отсюда, т.к. он пока не используется
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()