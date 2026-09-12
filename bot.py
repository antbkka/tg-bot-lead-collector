"""Telegram lead-collector на aiogram 3.x.

Бот проводит пользователя через короткую анкету из имени и задачи,
сохраняет заявку в SQLite, уведомляет администратора, позволяет ему
отвечать клиенту прямо в боте и принимает ответные сообщения клиента.
Конфигурация — через `.env`.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
LEAD_STATUS_NEW = "new"
LEAD_STATUS_NOTIFIED = "notified"
LEAD_STATUS_REPLIED = "replied"
logger = logging.getLogger(__name__)
router = Router()


@dataclass(frozen=True, slots=True)
class Settings:
    """Проверенные настройки приложения."""

    bot_token: str
    admin_chat_id: int
    database_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        """Загружает настройки и завершает запуск при неверной конфигурации."""

        load_dotenv(BASE_DIR / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        admin_id = os.getenv("ADMIN_CHAT_ID", "").strip()
        database_name = os.getenv("DATABASE_NAME", "leads.db").strip()

        if not token:
            raise RuntimeError("Не задан BOT_TOKEN в файле .env.")
        try:
            parsed_admin_id = int(admin_id)
        except ValueError as error:
            raise RuntimeError("ADMIN_CHAT_ID должен быть целым числом.") from error
        if not database_name or Path(database_name).name != database_name:
            raise RuntimeError("DATABASE_NAME должен содержать только имя файла.")

        return cls(token, parsed_admin_id, BASE_DIR / database_name)


class LeadForm(StatesGroup):
    """Состояния пошагового заполнения заявки."""

    name = State()
    task = State()
    confirmation = State()


class AdminReply(StatesGroup):
    """Состояние ввода ответа администратора на конкретную заявку."""

    text = State()


class Database:
    """Небольшой слой доступа к SQLite без внешней ORM."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Создаёт таблицу, индексы и применяет миграции схемы."""

        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_username TEXT,
                    name TEXT NOT NULL,
                    task TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new'
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at)"
            )
            self._migrate_legacy_schema(connection)

    @staticmethod
    def _migrate_legacy_schema(connection: sqlite3.Connection) -> None:
        """Приводит таблицу leads к актуальной схеме.

        Поддерживаются следующие миграции:

        1. старая таблица с колонками ``phone`` и ``task`` — пересоздаётся без ``phone``;
        2. старая таблица с колонкой ``notification_sent`` — данные переносятся в ``status``;
        3. отсутствие колонки ``status`` — добавляется с значением по умолчанию.
        """

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(leads)")}
        if not columns:
            return

        # Если в таблице остался устаревший столбец phone — пересоздаём таблицу.
        if "phone" in columns:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """
                CREATE TABLE leads_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_username TEXT,
                    name TEXT NOT NULL,
                    task TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new'
                )
                """
            )
            has_status_old = "status" in columns
            notification_expr = (
                "CASE WHEN notification_sent = 1 THEN ? ELSE 'new' END"
                if "notification_sent" in columns
                else "COALESCE(" + ("status" if has_status_old else "'new'") + ", 'new')"
            )
            connection.execute(
                f"""
                INSERT INTO leads_new (
                    id, telegram_user_id, telegram_username, name, task,
                    created_at, status
                )
                SELECT
                    id, telegram_user_id, telegram_username, name, task,
                    created_at, {notification_expr}
                FROM leads
                """,
                (LEAD_STATUS_NOTIFIED,),
            )
            connection.execute("DROP TABLE leads")
            connection.execute("ALTER TABLE leads_new RENAME TO leads")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_leads_created_at "
                "ON leads(created_at)"
            )
            connection.execute("PRAGMA foreign_keys=ON")
            return

        if "status" not in columns:
            connection.execute(
                "ALTER TABLE leads ADD COLUMN status TEXT NOT NULL DEFAULT 'new'"
            )

        if "notification_sent" in columns:
            connection.execute(
                "UPDATE leads SET status = ? WHERE status = 'new' AND notification_sent = 1",
                (LEAD_STATUS_NOTIFIED,),
            )

    def add_lead(
        self,
        telegram_user_id: int,
        telegram_username: str | None,
        name: str,
        task: str,
    ) -> int:
        """Сохраняет заявку и возвращает её ID."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO leads (
                    telegram_user_id, telegram_username, name, task,
                    created_at, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_user_id,
                    telegram_username,
                    name,
                    task,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    LEAD_STATUS_NEW,
                ),
            )
            return int(cursor.lastrowid)

    def update_status(self, lead_id: int, status: str) -> None:
        """Обновляет статус заявки."""

        with self._connect() as connection:
            connection.execute(
                "UPDATE leads SET status = ? WHERE id = ?", (status, lead_id)
            )

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        """Возвращает заявку по идентификатору или None."""

        with self._connect() as connection:
            return connection.execute(
                """
                SELECT id, telegram_user_id, telegram_username, name,
                       task, created_at, status
                FROM leads WHERE id = ?
                """,
                (lead_id,),
            ).fetchone()

    def get_lead_for_user(self, telegram_user_id: int) -> sqlite3.Row | None:
        """Возвращает последнюю заявку пользователя или None."""

        with self._connect() as connection:
            return connection.execute(
                """
                SELECT id, telegram_user_id, telegram_username, name,
                       task, created_at, status
                FROM leads
                WHERE telegram_user_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_user_id,),
            ).fetchone()

    def statistics(self) -> tuple[int, int, int, int]:
        """Возвращает всего / за сутки / новых / обработанных заявок."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(created_at >= datetime('now', '-1 day')) AS today,
                    SUM(status = 'new') AS pending,
                    SUM(status = 'replied') AS replied
                FROM leads
                """
            ).fetchone()
        return (
            int(row["total"]),
            int(row["today"] or 0),
            int(row["pending"] or 0),
            int(row["replied"] or 0),
        )

    def export_csv(self) -> bytes:
        """Формирует CSV со всеми заявками в памяти."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, created_at, name, task, telegram_username,
                       telegram_user_id, status
                FROM leads ORDER BY id DESC
                """
            ).fetchall()

        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(
            (
                "id",
                "created_at_utc",
                "name",
                "task",
                "telegram_username",
                "telegram_user_id",
                "status",
            )
        )
        writer.writerows(tuple(row) for row in rows)
        return stream.getvalue().encode("cp1251", errors="replace")


@dataclass(slots=True)
class AppContext:
    """Зависимости, доступные обработчикам через middleware data."""

    settings: Settings
    database: Database


class DependencyMiddleware:
    """Передаёт конфигурацию и БД в аргументы обработчиков."""

    def __init__(self, context: AppContext) -> None:
        self.context = context

    async def __call__(
        self,
        handler: Any,
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        data["app"] = self.context
        return await handler(event, data)


def confirmation_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает клавиатуру подтверждения анкеты."""

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Отправить заявку")],
            [KeyboardButton(text="🔄 Заполнить заново"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def reply_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Возвращает клавиатуру для режима ответа администратора."""

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="↩️ Отменить ответ")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def format_summary(data: dict[str, str]) -> str:
    """Формирует безопасное HTML-превью заявки."""

    return (
        "<b>Проверьте данные заявки:</b>\n\n"
        f"<b>Имя:</b> {escape(data['name'])}\n"
        f"<b>Задача:</b> {escape(data['task'])}"
    )


def is_admin(message: Message | CallbackQuery, app: AppContext) -> bool:
    """Проверяет права администратора по Telegram ID."""

    user = message.from_user
    return user is not None and user.id == app.settings.admin_chat_id


def admin_actions_keyboard(lead_id: int) -> InlineKeyboardMarkup:
    """Возвращает inline-клавиатуру под заявкой для администратора."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✉️ Ответить клиенту",
                    callback_data=f"reply:{lead_id}",
                )
            ]
        ]
    )


@router.message(CommandStart())
async def start_form(message: Message, state: FSMContext) -> None:
    """Сбрасывает старые данные и начинает анкету."""

    await state.clear()
    await state.set_state(LeadForm.name)
    await message.answer(
        "👋 <b>Здравствуйте!</b>\n\n"
        "Ответьте на пару коротких вопросов — менеджер получит заявку и свяжется с вами.\n\n"
        "<b>1 из 2.</b> Как вас зовут?",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("help"))
async def show_help(message: Message) -> None:
    """Показывает пользователю доступные команды."""

    await message.answer(
        "<b>Доступные команды</b>\n\n"
        "/start — заполнить заявку\n"
        "/cancel — отменить заполнение\n"
        "/help — показать справку"
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel_form(message: Message, state: FSMContext) -> None:
    """Отменяет заполнение на любом шаге."""

    active = await state.get_state()
    await state.clear()
    text = (
        "Заполнение отменено. Чтобы начать заново, отправьте /start."
        if active
        else "Активной анкеты нет. Чтобы начать, отправьте /start."
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(F.text == "🔄 Заполнить заново")
async def restart_form(message: Message, state: FSMContext) -> None:
    """Возвращает пользователя к первому вопросу."""

    await state.clear()
    await state.set_state(LeadForm.name)
    await message.answer("<b>1 из 2.</b> Как вас зовут?", reply_markup=ReplyKeyboardRemove())


@router.message(F.text == "↩️ Отменить ответ", AdminReply.text)
async def cancel_admin_reply(message: Message, state: FSMContext) -> None:
    """Отменяет ввод ответа администратора."""

    await state.clear()
    await message.answer("Ответ отменён.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("stats"))
async def admin_stats(message: Message, app: AppContext) -> None:
    """Показывает администратору статистику заявок."""

    if not is_admin(message, app):
        await message.answer("Команда доступна только администратору.")
        return
    try:
        total, today, pending, replied = await asyncio.to_thread(app.database.statistics)
    except sqlite3.Error:
        logger.exception("Не удалось получить статистику")
        await message.answer("Не удалось получить статистику.")
        return
    await message.answer(
        "📊 <b>Статистика заявок</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"За последние 24 часа: <b>{today}</b>\n"
        f"Новых без ответа: <b>{pending}</b>\n"
        f"С ответом менеджера: <b>{replied}</b>"
    )


@router.message(Command("export"))
async def admin_export(message: Message, app: AppContext) -> None:
    """Отправляет администратору резервную CSV-выгрузку."""

    if not is_admin(message, app):
        await message.answer("Команда доступна только администратору.")
        return
    try:
        content = await asyncio.to_thread(app.database.export_csv)
    except sqlite3.Error:
        logger.exception("Не удалось экспортировать заявки")
        await message.answer("Не удалось сформировать выгрузку.")
        return

    filename = f"leads_{datetime.now():%Y-%m-%d}.csv"
    await message.answer_document(
        BufferedInputFile(content, filename=filename),
        caption="Выгрузка заявок в формате CSV.",
    )


@router.message(LeadForm.name, F.text)
async def process_name(message: Message, state: FSMContext) -> None:
    """Проверяет имя и переходит к описанию задачи."""

    name = " ".join(message.text.split())
    if not 2 <= len(name) <= 100:
        await message.answer("Имя должно содержать от 2 до 100 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(LeadForm.task)
    await message.answer("<b>2 из 2.</b> Кратко опишите вашу задачу:")


@router.message(LeadForm.task, F.text)
async def process_task(message: Message, state: FSMContext) -> None:
    """Сохраняет описание во временное состояние и показывает превью."""

    task = message.text.strip()
    if not 3 <= len(task) <= 2000:
        await message.answer("Описание должно содержать от 3 до 2000 символов.")
        return
    await state.update_data(task=task)
    await state.set_state(LeadForm.confirmation)
    data = await state.get_data()
    await message.answer(format_summary(data), reply_markup=confirmation_keyboard())


@router.message(LeadForm.confirmation, F.text == "✅ Отправить заявку")
async def submit_lead(
    message: Message, state: FSMContext, bot: Bot, app: AppContext
) -> None:
    """Сохраняет подтверждённую заявку и уведомляет администратора."""

    user = message.from_user
    data = await state.get_data()
    if user is None or not {"name", "task"}.issubset(data):
        logger.warning("Неполные данные FSM при отправке заявки")
        await state.clear()
        await message.answer("Данные анкеты потеряны. Начните заново: /start")
        return

    try:
        lead_id = await asyncio.to_thread(
            app.database.add_lead,
            user.id,
            user.username,
            data["name"],
            data["task"],
        )
    except (sqlite3.Error, OSError):
        logger.exception("Ошибка сохранения заявки")
        await message.answer("Не удалось сохранить заявку. Попробуйте немного позже.")
        return

    username = f"@{escape(user.username)}" if user.username else "не указан"
    admin_text = (
        f"📩 <b>Новая заявка №{lead_id}</b>\n\n"
        f"<b>Имя:</b> {escape(data['name'])}\n"
        f"<b>Задача:</b> {escape(data['task'])}\n\n"
        f"<b>Telegram:</b> {username}\n"
        f"<b>User ID:</b> <code>{user.id}</code>"
    )

    try:
        await bot.send_message(
            app.settings.admin_chat_id,
            admin_text,
            reply_markup=admin_actions_keyboard(lead_id),
        )
        await asyncio.to_thread(app.database.update_status, lead_id, LEAD_STATUS_NOTIFIED)
    except (TelegramAPIError, sqlite3.Error):
        logger.exception("Ошибка уведомления для заявки №%s", lead_id)
        await message.answer(
            "✅ Заявка сохранена. Уведомление менеджеру задерживается, но данные не потеряны.",
            reply_markup=ReplyKeyboardRemove(),
        )
    else:
        await message.answer(
            "✅ <b>Спасибо!</b> Заявка принята. Мы свяжемся с вами.",
            reply_markup=ReplyKeyboardRemove(),
        )
    finally:
        await state.clear()


@router.message(LeadForm.name)
@router.message(LeadForm.task)
@router.message(LeadForm.confirmation)
async def invalid_form_input(message: Message) -> None:
    """Обрабатывает неподходящий тип сообщения на любом шаге."""

    await message.answer("Используйте текст или предложенные кнопки. Для отмены: /cancel")


@router.callback_query(F.data.startswith("reply:"))
async def start_admin_reply(
    callback: CallbackQuery, state: FSMContext, app: AppContext
) -> None:
    """Переводит администратора в режим ввода ответа клиенту."""

    if not is_admin(callback, app):
        await callback.answer("Недоступно.", show_alert=True)
        return
    try:
        lead_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный идентификатор.", show_alert=True)
        return

    lead = await asyncio.to_thread(app.database.get_lead, lead_id)
    if lead is None:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    await state.set_state(AdminReply.text)
    await state.update_data(lead_id=lead_id)
    await callback.answer()
    await callback.message.answer(
        f"✍️ <b>Ответ на заявку №{lead_id}</b>\n"
        f"Клиент: <b>{escape(lead['name'])}</b>\n\n"
        "Отправьте ответ одним сообщением или нажмите «Отменить ответ».",
        reply_markup=reply_cancel_keyboard(),
    )


@router.message(AdminReply.text, F.text)
async def send_admin_reply(
    message: Message, state: FSMContext, app: AppContext, bot: Bot
) -> None:
    """Отправляет ответ администратора клиенту и помечает заявку как обработанную."""

    data = await state.get_data()
    lead_id = data.get("lead_id")
    if not isinstance(lead_id, int):
        await state.clear()
        await message.answer("Состояние ответа потеряно.", reply_markup=ReplyKeyboardRemove())
        return

    lead = await asyncio.to_thread(app.database.get_lead, lead_id)
    if lead is None:
        await state.clear()
        await message.answer("Заявка не найдена.", reply_markup=ReplyKeyboardRemove())
        return

    reply_text = message.text.strip()
    if not 1 <= len(reply_text) <= 4000:
        await message.answer("Ответ должен содержать от 1 до 4000 символов.")
        return

    user_text = (
        f"💬 <b>Ответ менеджера по заявке №{lead_id}</b>\n\n"
        f"{escape(reply_text)}\n\n"
        "Если остались вопросы — просто напишите их сюда."
    )
    try:
        await bot.send_message(lead["telegram_user_id"], user_text)
    except TelegramAPIError:
        logger.exception("Не удалось доставить ответ клиенту по заявке №%s", lead_id)
        await message.answer(
            "Не удалось отправить ответ клиенту — он мог заблокировать бота.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    try:
        await asyncio.to_thread(
            app.database.update_status, lead_id, LEAD_STATUS_REPLIED
        )
    except sqlite3.Error:
        logger.exception("Не удалось обновить статус заявки №%s", lead_id)

    await message.answer(
        f"✅ Ответ по заявке №{lead_id} отправлен клиенту.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.clear()


@router.message(AdminReply.text)
async def invalid_admin_reply(message: Message) -> None:
    """Просит прислать ответ обычным текстом."""

    await message.answer("Отправьте ответ текстом или нажмите «↩️ Отменить ответ».")


async def _forward_user_message_to_admin(
    message: Message, app: AppContext
) -> bool:
    """Пересылает сообщение клиента админу, привязывая к заявке.

    Возвращает True, если сообщение удалось переслать.
    """

    user = message.from_user
    if user is None:
        return False

    try:
        lead = await asyncio.to_thread(
            app.database.get_lead_for_user, user.id
        )
    except sqlite3.Error:
        logger.exception("Ошибка поиска заявки для пересылки")
        lead = None

    if lead is None:
        return False

    username = f"@{escape(user.username)}" if user.username else "не указан"
    header = (
        f"📨 <b>Сообщение клиента по заявке №{lead['id']}</b>\n"
        f"<b>Имя:</b> {escape(lead['name'])}\n"
        f"<b>Telegram:</b> {username} (<code>{user.id}</code>)\n\n"
    )
    try:
        await message.bot.send_message(
            app.settings.admin_chat_id,
            header,
            reply_markup=admin_actions_keyboard(lead["id"]),
        )
        await message.forward(app.settings.admin_chat_id)
    except TelegramAPIError:
        logger.exception("Не удалось переслать сообщение клиента админу")
        return False
    return True


@router.message()
async def forward_or_unknown(message: Message, app: AppContext) -> None:
    """Пересылает сообщения клиентов админу или подсказывает /start."""

    if message.from_user is None or message.text is None:
        return

    if await _forward_user_message_to_admin(message, app):
        await message.answer(
            "✅ Сообщение отправлено менеджеру. Ожидайте ответа."
        )
        return

    await message.answer("Чтобы оставить заявку, отправьте /start. Справка: /help")


@router.error()
async def handle_error(event: ErrorEvent) -> bool:
    """Логирует необработанные исключения без раскрытия деталей пользователю."""

    logger.error(
        "Необработанная ошибка при обработке update_id=%s",
        event.update.update_id,
        exc_info=event.exception,
    )
    if event.update.message:
        try:
            await event.update.message.answer(
                "Произошла внутренняя ошибка. Попробуйте ещё раз позже."
            )
        except TelegramAPIError:
            logger.exception("Не удалось сообщить пользователю об ошибке")
    return True


async def set_bot_commands(bot: Bot) -> None:
    """Регистрирует меню команд в интерфейсе Telegram."""

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Оставить заявку"),
            BotCommand(command="cancel", description="Отменить заполнение"),
            BotCommand(command="help", description="Помощь"),
        ]
    )


async def main() -> None:
    """Создаёт приложение и запускает long polling."""

    settings = Settings.from_env()
    database = Database(settings.database_path)
    await asyncio.to_thread(database.initialize)
    context = AppContext(settings=settings, database=database)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.update.middleware(DependencyMiddleware(context))
    dispatcher.include_router(router)

    try:
        await set_bot_commands(bot)
        logger.info("Бот запущен; база данных: %s", settings.database_path.name)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        logger.info("Сессия бота закрыта")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
    except RuntimeError as error:
        logger.critical("Ошибка конфигурации: %s", error)
