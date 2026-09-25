"""User interface strings, English and Russian.

The GUI can switch language at runtime: :class:`Translator` emits
``languageChanged`` and every widget re-applies its text from that signal, so
adding a language means adding one dictionary here (English is the reference;
a missing Russian key silently falls back to English).
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .qtcompat import QObject, Signal

__all__ = ["Translator", "tr", "LANGUAGES", "available_languages"]

LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ru", "Русский"),
)

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        # -- window / shell -------------------------------------------------
        "app.title": "flacblind — blind ABX test",
        "app.subtitle": "Can you actually hear the difference?",
        "app.ready": "Ready",
        "app.idle": "Drop two versions of the same track to begin",
        # -- sources --------------------------------------------------------
        "sources.title": "Sources",
        "sources.lossless": "Lossless source",
        "sources.lossy": "Lossy source",
        "sources.hint.lossless": "FLAC · WAV · AIFF · ALAC",
        "sources.hint.lossy": "MP3 · AAC · Ogg · Opus",
        "sources.drop": "Drop audio files here",
        "sources.browse": "Click to browse…",
        "sources.change": "Change…",
        "sources.clear": "Clear",
        "sources.swap": "Swap A / B",
        "sources.auto": "auto-detected",
        "sources.same_file": "Pick two different files.",
        "sources.recent": "Recent files",
        # -- processing -----------------------------------------------------
        "prepare.title": "Prepare",
        "prepare.processing": "Audio processing",
        "prepare.loudness": "Loudness matching (EBU R128)",
        "prepare.loudness.tip": "Two-pass loudnorm: both files land on exactly the same integrated loudness, so level can never be mistaken for quality.",
        "prepare.target": "Target loudness",
        "prepare.true_peak": "True peak",
        "prepare.lra": "Loudness range",
        "prepare.rate": "Output sample rate",
        "prepare.start": "Prepare audio",
        "prepare.reprepare": "Re-prepare",
        "prepare.cancel": "Cancel",
        "prepare.busy": "Processing audio…",
        "prepare.levels": "Levels",
        "prepare.segment": "Test segment",
        "prepare.segment.whole": "Whole file",
        "prepare.segment.custom": "Custom segment",
        "prepare.segment.start": "Start",
        "prepare.segment.length": "Length",
        "prepare.segment.hint": "Compare only part of the track — often easier to judge.",
        "prepare.segment.jump": "Jump to the loudest part",
        "prepare.seconds": "s",
        # -- session --------------------------------------------------------
        "session.title": "Session",
        "session.rounds": "Rounds",
        "session.rounds.n": "{n} rounds",
        "session.rounds.endless": "Endless",
        "session.reveal": "Reveal after each round",
        "session.reveal.tip": "Off = fully blind: the answer is only shown at the end.",
        "session.require_both": "Require both samples first",
        "session.seed": "Seed",
        "session.seed.tip": "Same seed = same A/B order. Leave empty for random.",
        "session.new": "New session",
        "session.round": "Round {current} / {total}",
        "session.round_endless": "Round {current}",
        "session.next": "Next round",
        "session.finish": "Finish",
        "session.skip": "Skip round",
        "session.start_playing": "Play a sample to begin",
        # -- player ---------------------------------------------------------
        "player.play": "Play / pause",
        "player.stop": "Stop",
        "player.loop": "Loop segment",
        "player.volume": "Volume",
        "player.backend": "Playback",
        "player.backend.sounddevice": "Sample-accurate (sounddevice)",
        "player.backend.qt": "Qt Multimedia",
        "player.backend.cli": "External player",
        "player.backend.none": "Unavailable",
        "player.backend.auto": "Automatic (best available)",
        "player.sample": "Sample {key}",
        "player.position": "Position",
        "player.seek_hint": "Click anywhere in the waveform to jump there.",
        "player.switching": "Position-locked A/B switching",
        "player.sample_accurate": "sample-accurate",
        "player.reduced": "no seek support",
        "player.mute": "Mute",
        "player.view": "Waveform view",
        "player.view.wave": "Waveform",
        "player.view.diff": "A − B difference",
        "player.view.diff.tip": "Where the two renders differ, sample by sample.",
        # -- voting ---------------------------------------------------------
        "vote.title": "Which one is lossless?",
        "vote.a": "A is lossless",
        "vote.b": "B is lossless",
        "vote.shortcut": "Press 1 for A, 2 for B",
        "vote.correct": "Correct!",
        "vote.wrong": "Not this time",
        "vote.truth": "The lossless sample was {key}",
        "vote.need_both": "Play A and B before voting.",
        "vote.need_plays": "Listen a little longer first.",
        "vote.already": "Already answered.",
        # -- results --------------------------------------------------------
        "results.title": "Results",
        "results.score": "Score",
        "results.pvalue": "p-value",
        "results.chance": "Chance of this score",
        "results.needed": "You need {n} more correct {plural} for 95 % confidence.",
        "results.needed.never": "Even a perfect score cannot reach 95 % confidence in {n} rounds.",
        "results.interval": "95 % interval",
        "results.z": "z-score",
        "results.verdict.strong": "Clearly audible",
        "results.verdict.significant": "Statistically significant",
        "results.verdict.strong": "Clearly audible",
        "results.verdict.weak": "Inconclusive",
        "results.verdict.none": "Indistinguishable",
        "results.detail.strong": (
            "Scoring at least this well by pure luck has a probability of only {pct} % — "
            "the difference is real and you can hear it."
        ),
        "results.detail.significant": (
            "A listener with no hearing at all would beat this score in fewer than 1 test out of 20 "
            "({pct} %). Real, though not overwhelming."
        ),
        "results.detail.weak": (
            "This score still happens {pct} % of the time by accident. "
            "Play a few more rounds to be sure."
        ),
        "results.detail.none": (
            "A coin flip would score this well {pct} % of the time — "
            "the two files are effectively identical to your ears."
        ),
        "results.history": "History",
        "results.col.round": "#",
        "results.col.guess": "You",
        "results.col.truth": "Lossless",
        "results.export": "Export…",
        "results.exported": "Saved to {path}",
        "results.export_error": "Could not write the report: {error}",
        "results.distribution": "Null distribution",
        "results.distribution.tip": "How a listener with no hearing at all would score. The marker is your result.",
        "results.no_data": "No rounds played yet",
        "results.wins": "wins",
        "results.losses": "losses",
        # -- messages -------------------------------------------------------
        "msg.files_missing": "Choose a lossless and a lossy file first.",
        "msg.ffmpeg_missing": "ffmpeg is required for conversion, but was not found.",
        "msg.preparing": "Preparing audio…",
        "msg.ready": "Ready — press A or B to start listening.",
        "msg.temp_cleaned": "Removed {n} leftover temporary folder(s).",
        # -- settings / dialogs ---------------------------------------------
        "settings.delete_temp": "Delete temporary WAVs on exit",
        "settings.auto_advance": "Auto-advance",
        "settings.auto_advance.tip": "Show the next round automatically a moment after voting.",
        "settings.delete_temp.tip": "Leftovers from a crashed session are also removed on the next start.",
        "shortcuts.title": "Keyboard shortcuts",
        "shortcuts.space": "Play / pause the active sample",
        "shortcuts.a": "Play sample A",
        "shortcuts.b": "Play sample B",
        "shortcuts.1": "Vote: A is lossless",
        "shortcuts.2": "Vote: B is lossless",
        "shortcuts.arrows": "Seek ±5 s (hold Shift for ±15 s)",
        "shortcuts.esc": "Pause playback",
        "shortcuts.n": "Next round",
        "shortcuts.m": "Mute / unmute",
        "about.title": "About flacblind",
        "about.text": (
            "A blind ABX test for lossless vs. lossy audio.\n\n"
            "Both files are loudness-matched with a two-pass EBU R128 normalisation, "
            "so a louder master can never be mistaken for a better one. The verdict "
            "is an exact binomial test, not a gut feeling."
        ),
        "about.version": "Version {version} · {binding}",
        # -- menu ------------------------------------------------------------
        "menu.file": "File",
        "menu.session": "Session",
        "menu.view": "View",
        "menu.help": "Help",
        "menu.open": "Open audio files…",
        "menu.export": "Export results…",
        "menu.quit": "Quit",
        "menu.shortcuts": "Keyboard shortcuts",
        "menu.about": "About",
        # -- generic ---------------------------------------------------------
        "common.action_failed": "The action “{action}” failed.",
        "common.filter": "Audio files",
        "common.all_files": "All files",
    },
    "ru": {
        "app.title": "flacblind — слепой тест ABX",
        "app.subtitle": "Слышите ли вы разницу на самом деле?",
        "app.ready": "Готово",
        "app.idle": "Перетащите две версии одного трека, чтобы начать",
        "sources.title": "Источники",
        "sources.lossless": "Без потерь",
        "sources.lossy": "С потерями",
        "sources.hint.lossless": "FLAC · WAV · AIFF · ALAC",
        "sources.hint.lossy": "MP3 · AAC · Ogg · Opus",
        "sources.drop": "Перетащите аудиофайлы сюда",
        "sources.browse": "Нажмите, чтобы выбрать…",
        "sources.change": "Изменить…",
        "sources.clear": "Очистить",
        "sources.swap": "Поменять A / B",
        "sources.auto": "определено автоматически",
        "sources.same_file": "Выберите два разных файла.",
        "sources.recent": "Недавние файлы",
        "prepare.title": "Подготовка",
        "prepare.processing": "Обработка звука",
        "prepare.loudness": "Выравнивание громкости (EBU R128)",
        "prepare.loudness.tip": "Двухпроходный loudnorm: оба файла получают одинаковую интегрированную громкость, поэтому разница в уровне не выдаётся за разницу в качестве.",
        "prepare.target": "Целевая громкость",
        "prepare.true_peak": "Истинный пик",
        "prepare.lra": "Диапазон громкости",
        "prepare.rate": "Частота дискретизации",
        "prepare.start": "Подготовить",
        "prepare.reprepare": "Подготовить заново",
        "prepare.cancel": "Отмена",
        "prepare.busy": "Обработка аудио…",
        "prepare.levels": "Уровни",
        "prepare.segment": "Участок теста",
        "prepare.segment.whole": "Файл целиком",
        "prepare.segment.custom": "Свой участок",
        "prepare.segment.start": "Начало",
        "prepare.segment.length": "Длительность",
        "prepare.segment.hint": "Сравнивайте только часть трека — так часто легче заметить разницу.",
        "prepare.segment.jump": "Перейти к самому громкому месту",
        "prepare.seconds": "с",
        "msg.files_missing": "Сначала выберите файлы без потерь и с потерями.",
        "msg.ffmpeg_missing": "Для конвертации нужен ffmpeg, но он не найден.",
        "msg.preparing": "Обработка аудио…",
        "msg.ready": "Готово — нажмите A или B, чтобы начать слушать.",
        "msg.temp_cleaned": "Удалено старых временных папок: {n}.",
        "sources.same_file": "Выберите два разных файла.",
        "sources.recent": "Недавние файлы",
        "session.title": "Сессия",
        "session.rounds": "Раундов",
        "session.rounds.n": "{n} раундов",
        "session.rounds.endless": "Бесконечно",
        "session.reveal": "Ответ после раунда",
        "session.reveal.tip": "Выключено = полностью слепой тест: ответ только в конце.",
        "session.require_both": "Сначала послушать оба",
        "session.seed": "Зерно",
        "session.seed.tip": "Одинаковое зерно = одинаковый порядок A/B. Пусто — случайно.",
        "session.new": "Новая сессия",
        "session.round": "Раунд {current} / {total}",
        "session.round_endless": "Раунд {current}",
        "session.next": "Следующий раунд",
        "session.finish": "Завершить",
        "session.skip": "Пропустить раунд",
        "session.start_playing": "Включите образец, чтобы начать",
        "player.play": "Играть / пауза",
        "player.stop": "Стоп",
        "player.loop": "Повтор участка",
        "player.volume": "Громкость",
        "player.backend": "Воспроизведение",
        "player.backend.sounddevice": "Сэмпловая точность (sounddevice)",
        "player.backend.qt": "Qt Multimedia",
        "player.backend.cli": "Внешний проигрыватель",
        "player.backend.none": "Недоступно",
        "player.backend.auto": "Автоматически (лучший доступный)",
        "player.sample": "Образец {key}",
        "player.position": "Позиция",
        "player.seek_hint": "Щёлкните по волне, чтобы перейти к моменту.",
        "player.switching": "переключение A/B с сохранением позиции",
        "player.sample_accurate": "сэмпловая точность",
        "player.reduced": "без перемотки",
        "player.mute": "Звук",
        "player.view": "Вид волны",
        "player.view.wave": "Волна",
        "player.view.diff": "Разница A − B",
        "player.view.diff.tip": "Где два рендера расходятся, сэмпл за сэмплом.",
        "vote.title": "Какой образец без потерь?",
        "vote.a": "A — без потерь",
        "vote.b": "B — без потерь",
        "vote.shortcut": "Нажмите 1 для A, 2 для B",
        "vote.correct": "Верно!",
        "vote.wrong": "Не в этот раз",
        "vote.truth": "Без потерь был образец {key}",
        "vote.need_both": "Прослушайте A и B перед голосованием.",
        "vote.need_plays": "Сначала послушайте подольше.",
        "vote.already": "Голос уже учтён.",
        "results.title": "Результаты",
        "results.score": "Счёт",
        "results.pvalue": "p-value",
        "results.chance": "Вероятность такого счёта",
        "results.needed": "Нужно ещё {n} верн. {plural} для 95 % достоверности.",
        "results.needed.never": "Даже идеальный счёт не даёт 95 % достоверности за {n} раундов.",
        "results.interval": "95 % интервал",
        "results.z": "z-значение",
        "results.verdict.strong": "Разница отчётливо слышна",
        "results.verdict.significant": "Статистически значимо",
        "results.verdict.strong": "Разница отчётливо слышна",
        "results.verdict.weak": "Неоднозначно",
        "results.verdict.none": "Неразличимо",
        "results.detail.strong": (
            "Случайно набрать столько же можно лишь с вероятностью {pct} % — "
            "разница реальна и вы её слышите."
        ),
        "results.detail.significant": (
            "Случайный слушатель превзойдёт этот счёт менее чем в 1 тесте из 20 ({pct} %). "
            "Разница настоящая, хотя и не абсолютная."
        ),
        "results.detail.weak": (
            "Такой счёт случайно получается в {pct} % случаев. Сыграйте ещё несколько раундов."
        ),
        "results.detail.none": (
            "Подбрасыванием монеты такой счёт выпадает в {pct} % случаев — "
            "файлы для вашего слуха практически идентичны."
        ),
        "results.history": "История",
        "results.col.round": "#",
        "results.col.guess": "Вы",
        "results.col.truth": "Без потерь",
        "results.export": "Экспорт…",
        "results.exported": "Сохранено в {path}",
        "results.export_error": "Не удалось сохранить отчёт: {error}",
        "results.distribution": "Распределение при случайности",
        "results.distribution.tip": "Такой счёт мог бы выпасть у человека без слуха. Отметка — ваш результат.",
        "results.no_data": "Раундов пока нет",
        "results.wins": "верных",
        "results.losses": "ошибок",
        "settings.delete_temp": "Удалять временные WAV при выходе",
        "settings.auto_advance": "Автопереход",
        "settings.auto_advance.tip": "Показывать следующий раунд сразу после голосования.",
        "settings.delete_temp.tip": "Оставшиеся файлы после сбоя тоже удаляются при следующем запуске.",
        "shortcuts.title": "Горячие клавиши",
        "shortcuts.space": "Играть / пауза текущего образца",
        "shortcuts.a": "Играть образец A",
        "shortcuts.b": "Играть образец B",
        "shortcuts.1": "Голос: A без потерь",
        "shortcuts.2": "Голос: B без потерь",
        "shortcuts.arrows": "Перемотка ±5 с (Shift — ±15 с)",
        "shortcuts.esc": "Пауза",
        "shortcuts.n": "Следующий раунд",
        "shortcuts.m": "Включить / выключить звук",
        "about.title": "О программе flacblind",
        "about.text": (
            "Слепой тест ABX для сравнения файлов с потерями и без.\n\n"
            "Оба файла выравниваются по громкости двухпроходным нормализатором EBU R128, "
            "поэтому более громкий мастер нельзя принять за более качественный. "
            "Итоговый вывод — точный биномиальный тест, а не субъективное ощущение."
        ),
        "about.version": "Версия {version} · {binding}",
        "menu.file": "Файл",
        "menu.session": "Сессия",
        "menu.view": "Вид",
        "menu.help": "Справка",
        "menu.open": "Открыть аудиофайлы…",
        "menu.export": "Экспортировать результаты…",
        "menu.quit": "Выход",
        "menu.shortcuts": "Горячие клавиши",
        "menu.about": "О программе",
        "common.action_failed": "Действие «{action}» завершилось ошибкой.",
        "common.filter": "Аудиофайлы",
        "common.all_files": "Все файлы",
    },
}

#: Plural helper for languages with gendered plurals (Russian).
_FORMS: dict[str, tuple[str, str, str]] = {
    "en": ("vote", "votes", "votes"),
    "ru": ("верный ответ", "верных ответа", "верных ответов"),
}


class Translator(QObject):
    """Tiny runtime translator with change notification."""

    languageChanged = Signal()

    _instance: "Translator | None" = None

    def __init__(self, language: str = "en", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._language = language if language in _STRINGS else "en"
        self._callbacks: list[Callable[[], None]] = []

    @classmethod
    def instance(cls) -> "Translator":
        if cls._instance is None:
            cls._instance = Translator()
        return cls._instance

    # ------------------------------------------------------------------ api
    @property
    def language(self) -> str:
        return self._language

    @staticmethod
    def available() -> list[tuple[str, str]]:
        return list(LANGUAGES)

    def set_language(self, language: str) -> None:
        if language not in _STRINGS or language == self._language:
            return
        self._language = language
        self.languageChanged.emit()
        for callback in list(self._callbacks):
            callback()

    def toggle_language(self) -> None:
        codes = [code for code, _ in LANGUAGES]
        index = codes.index(self._language) if self._language in codes else 0
        self.set_language(codes[(index + 1) % len(codes)])

    def tr(self, _key: str, **kwargs: Any) -> str:
        # The first parameter is underscore-prefixed so that a translation
        # key can use a ``{key}`` placeholder: tr("player.sample", key="A").
        key = _key
        text = _STRINGS.get(self._language, {}).get(key) or _STRINGS["en"].get(key) or key
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):  # pragma: no cover - defensive
            return text

    def plural(self, n: int, forms: Iterable[str] = ()) -> str:
        """Russian needs three plural forms; English two are enough."""
        if forms:
            one, few, many = tuple(forms)  # type: ignore[misc]
        else:
            one, few, many = _FORMS.get(self._language, _FORMS["en"])
        n = abs(int(n))
        if self._language == "ru":
            if n % 10 == 1 and n % 100 != 11:
                return one
            if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
                return few
            return many
        return one if n == 1 else few

    def on_change(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a retranslate callback; returns a de-registration hook."""
        self._callbacks.append(callback)
        return lambda: self._unregister(callback)

    def _unregister(self, callback: Callable[[], None]) -> None:
        with_index = getattr(self._callbacks, "index", None)
        if with_index is None:  # pragma: no cover - defensive
            return
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass


def _make_proxy() -> Callable[..., str]:
    """Module level ``tr()`` bound to the singleton translator."""

    def tr(_key: str, **kwargs: Any) -> str:
        return Translator.instance().tr(_key, **kwargs)

    return tr


tr = _make_proxy()


def available_languages() -> list[tuple[str, str]]:
    return list(LANGUAGES)
