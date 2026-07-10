# -*- coding: utf-8 -*-
"""Загрузка версионируемых конфигов артикуляций (config/articulation_maps/*.yaml).

Маппинги артикуляций больше не захардкожены в коде: при обновлении версии
инструмента или добавлении нового (например, Solemn Tones Odin) создаётся/
правится YAML-файл, а не логика в коде. Какой конфиг использован для трека —
явно логируется при экспорте (см. log_config_used).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("gpmidi.articulation")
if not logger.handlers and not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

CONFIG_DIR = Path(__file__).resolve().parent / "config" / "articulation_maps"

# Тип трека -> имя конфига (версия зашита в имени файла, как и задумано:
# обновление библиотеки = новый файл, а не правка логики).
CONFIG_BY_TRACK_TYPE = {
    "GUITAR": "shreddage_hydra_3.5",
    "BASS": "shreddage_darkwall_3.5",
    "DRUMS": "shreddage_drums",
}


@lru_cache(maxsize=None)
def load_config(name: str) -> dict:
    """Загрузить конфиг по имени файла (без .yaml). Кэшируется."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Конфиг артикуляций не найден: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    data["_config_name"] = name
    data["_config_path"] = str(path)
    return data


def config_for_track_type(track_type: str) -> dict | None:
    """Конфиг для типа трека (GUITAR/BASS/DRUMS) или None (OTHER)."""
    name = CONFIG_BY_TRACK_TYPE.get(track_type)
    return load_config(name) if name else None


def config_label(cfg: dict) -> str:
    return (
        f"{cfg.get('instrument', cfg['_config_name'])} "
        f"v{cfg.get('library_version', '?')} "
        f"(config_version {cfg.get('config_version', '?')}, {cfg['_config_name']}.yaml)"
    )


def log_config_used(track_name: str, cfg: dict | None) -> str:
    """Залогировать, какая версия конфига использована для трека при экспорте."""
    if cfg is None:
        msg = f"трек {track_name!r}: без маппинга артикуляций (OTHER)"
    else:
        msg = f"трек {track_name!r}: конфиг {config_label(cfg)}"
    logger.info(msg)
    return msg


def keyswitch_note(cfg: dict, articulation: str) -> int:
    """MIDI-нота keyswitch-а для артикуляции по имени."""
    ks = cfg["keyswitches"][articulation]
    return int(ks["note"])


def articulation_priority(cfg: dict) -> dict[str, int]:
    """Имя артикуляции -> приоритет (меньше = важнее)."""
    return {name: i for i, name in enumerate(cfg.get("articulation_priority", []))}


def drum_note_out(cfg: dict, gm_note: int) -> tuple[int | None, str | None]:
    """GM-нота из GP-percussion-трека -> (нота Shreddage Drums, warning|None).

    Порядок: явный remap -> identity (если нота в kit_layout) -> флэмы по
    формуле -> unmapped_policy (warn: как есть + предупреждение; drop: None).
    """
    remap = cfg.get("remap") or {}
    kit = cfg.get("kit_layout") or {}
    warnings_map = cfg.get("warnings") or {}
    flams = cfg.get("flams") or {}

    warn = warnings_map.get(gm_note)

    if gm_note in remap:
        return int(remap[gm_note]), warn
    if gm_note in kit:
        return gm_note, warn

    # флэмы томов: base_tom_note + offset
    offset = int(flams.get("offset", 24))
    toms = set(flams.get("toms") or [])
    base = gm_note - offset
    if base in toms:
        return gm_note, warn  # уже валидная флэм-нота (base+24)

    if (cfg.get("unmapped_policy") or "warn") == "drop":
        return None, (warn or f"GM-нота {gm_note} вне раскладки Shreddage Drums — нота ПРОПУЩЕНА")
    return gm_note, (warn or f"GM-нота {gm_note} вне раскладки Shreddage Drums — экспортирована как есть (может молчать в ките)")


def flam_note(cfg: dict, base_tom_note: int) -> int:
    """Нота флэма для тома: base_tom_note + offset (формула из мануала)."""
    flams = cfg.get("flams") or {}
    toms = set(flams.get("toms") or [])
    if base_tom_note not in toms:
        raise ValueError(f"{base_tom_note} не является томом из flams.toms")
    return base_tom_note + int(flams.get("offset", 24))
