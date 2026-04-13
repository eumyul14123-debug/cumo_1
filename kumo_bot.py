import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite
from datetime import datetime, timedelta
import random, re
from typing import Optional
import os

OFFLINE_DICT_PATH = "kor_offline_dict.txt"
OFFLINE_DICT_AUTOINIT = True
OFFLINE_DICT_MIN_SIZE = 1000

SPECIAL_BYPASS_USER_ID = 1382945340033863830
DESIGNER_ROLE_ID = 1317188290134282391
TICKET_CATEGORY_ID = 1417805627018838078

HOUSE_WIN_PROB_COIN = 0.45
RPS_BOT_WIN_PROB = 0.40
RPS_TIE_PROB = 0.30

CHOSUNG_WORDS_PATH = "chosung_words.txt"
QUIZ_TIME_LIMIT_SECONDS = 15
QUIZ_COOLDOWN_SECONDS = 15
QUIZ_CMD_COOLDOWN_SECONDS = 60
QUIZ_DAILY_LIMIT = 5
QUIZ_REWARD = 15

_active_quiz_sessions: dict[int, dict] = {}
_quiz_cooldowns: dict[int, float] = {}
_cached_words: list[str] | None = None

def _weighted_choice(items: list[str], weights: list[float]) -> str:
    assert len(items) == len(weights) and len(items) > 0
    total = sum(weights)
    r = random.random() * total
    upto = 0.0
    for item, w in zip(items, weights):
        if upto + w >= r:
            return item
        upto += w
    return items[-1]

def get_top_users(conn, limit=10, offset=0):
    cur = conn.cursor()
    cur.execute(
        "SELECT discord_id, mangul FROM users ORDER BY mangul DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return cur.fetchall()

def get_my_rank_and_score(conn, discord_id: int):
    cur = conn.cursor()
    cur.execute("SELECT mangul FROM users WHERE discord_id = ?", (discord_id,))
    row = cur.fetchone()
    if not row:
        return None, 0

    my = row[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE mangul > ?", (my,))
    higher = cur.fetchone()[0]
    rank = higher + 1
    return rank, my

_rank_cache = {}

def get_top_users_cached(conn, page, page_size=10, ttl=60):
    import time
    now = time.time()
    key = (page, page_size)
    if key in _rank_cache and _rank_cache[key][0] > now:
        return _rank_cache[key][1]
    offset = (page - 1) * page_size
    rows = get_top_users(conn, page_size, offset)
    _rank_cache[key] = (now + ttl, rows)
    return rows

def build_rank_embed(guild, top_rows, my_rank, my_score, page=1, page_size=10, kst_now=None):
    import datetime
    kst_now = kst_now or datetime.datetime.utcnow()
    title = "🏆 망울 랭킹 TOP 5"
    desc = "가장 많은 망울을 보유한 유저 TOP 5"

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    start_idx = (page - 1) * page_size
    for i, (uid, score) in enumerate(top_rows, start=1):
        rank_no = start_idx + i
        icon = medals[rank_no-1] if rank_no <= 3 else f"#{rank_no}"
        member = guild.get_member(uid) if guild else None
        name = member.mention if member else "탈퇴 유저"
        lines.append(f"{icon} {name} · {score:,} 망울")

    if not lines:
        lines = ["데이터가 아직 없어요! (•́₍₍๑•́‧̫•̀๑₎₎•̀)"]

    my_line = f"**내 순위:** #{my_rank} · {my_score:,} 망울" if my_rank else "**내 순위:** 등록되지 않음"

    embed = discord.Embed(title=title, description=desc)
    embed.add_field(name="순위표", value="\n".join(lines), inline=False)
    embed.add_field(name="\u200b", value=my_line, inline=False)
    embed.set_footer(text=f"갱신 시각(KST) · 페이지 {page}")
    return embed


async def _get_balance(db, user_id: int) -> int:
    cur = await db.execute("SELECT mangul FROM users WHERE discord_id=?", (user_id,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def ensure_quiz_schema(db: aiosqlite.Connection):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_usage (
            discord_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            used_count INTEGER NOT NULL DEFAULT 0,
            last_used_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (discord_id, date)
        );
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_quiz_usage_user ON quiz_usage(discord_id);")

async def ensure_offline_dict_schema(db: aiosqlite.Connection):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_dict (
            word TEXT PRIMARY KEY
        );
        """
    )

async def is_in_offline_dictionary(db: aiosqlite.Connection, word: str) -> bool:
    cur = await db.execute("SELECT 1 FROM offline_dict WHERE word=?", (word,))
    return (await cur.fetchone()) is not None

async def load_offline_dict_from_file(db: aiosqlite.Connection, path: str = OFFLINE_DICT_PATH) -> int:
    if not os.path.exists(path):
        return 0
    inserted = 0
    buf: list[tuple[str]] = []
    CHUNK = 1000
    def _flush_buf_sync(buf_local: list[tuple[str]]):
        pass
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            w = line.strip()
            if not w or not _is_korean_word(w):
                continue
            buf.append((w,))
            if len(buf) >= CHUNK:
                await db.executemany("INSERT OR IGNORE INTO offline_dict(word) VALUES (?)", buf)
                await db.commit()
                inserted += len(buf)
                buf.clear()
        if buf:
            await db.executemany("INSERT OR IGNORE INTO offline_dict(word) VALUES (?)", buf)
            await db.commit()
            inserted += len(buf)
            buf.clear()
    cur = await db.execute("SELECT COUNT(*) FROM offline_dict")
    total = (await cur.fetchone())[0]
    return int(total)

DB_PATH = "mangul.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id INTEGER UNIQUE NOT NULL,
            mangul INTEGER NOT NULL DEFAULT 0,
            last_attendance TEXT,
            attendance_days INTEGER NOT NULL DEFAULT 0
        )
        """)
        await db.commit()

async def ensure_designer_schema(db: aiosqlite.Connection):
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS designer_applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            activity_name TEXT NOT NULL,
            server_name TEXT NOT NULL,
            experience TEXT NOT NULL,
            ticket_channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            decided_at TEXT,
            decided_by INTEGER,
            decision_reason TEXT,
            moderation_message_id INTEGER
        );
        """
    )
    try:
        await db.execute("ALTER TABLE designer_applications ADD COLUMN moderation_message_id INTEGER")
    except Exception:
        pass
    await db.execute("CREATE INDEX IF NOT EXISTS idx_da_msg ON designer_applications(moderation_message_id);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_da_discord_id ON designer_applications(discord_id);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_da_status ON designer_applications(status);")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_da_ticket ON designer_applications(ticket_channel_id);")

async def get_or_create_user(db, discord_id: int):
    cur = await db.execute("SELECT mangul FROM users WHERE discord_id=?", (discord_id,))
    row = await cur.fetchone()
    await cur.close()
    if row:
        return row[0]
    else:
        await db.execute("INSERT INTO users(discord_id, mangul, attendance_days) VALUES (?, 0, 0)", (discord_id,))
        await db.commit()
        return 0

async def update_mangul(db, discord_id: int, amount: int):
    cur = await db.execute("SELECT mangul FROM users WHERE discord_id=?", (discord_id,))
    row = await cur.fetchone()
    if row:
        new_val = max(0, row[0] + amount)
        await db.execute("UPDATE users SET mangul=? WHERE discord_id=?", (new_val, discord_id))
    else:
        new_val = max(0, amount)
        await db.execute("INSERT INTO users(discord_id, mangul, attendance_days) VALUES (?, ?, 0)", (discord_id, new_val))
    await db.commit()
    return new_val

class RankPaginationView(discord.ui.View):
    def __init__(self, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.page = 1

    async def _build_embed(self, interaction: discord.Interaction) -> discord.Embed:
        PAGE_SIZE = 5
        offset = 0

        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                """
                SELECT discord_id, mangul
                FROM users
                ORDER BY mangul DESC
                LIMIT ? OFFSET ?
                """,
                (PAGE_SIZE, offset),
            )
            top_rows = await cur.fetchall()

            cur = await db.execute("SELECT mangul FROM users WHERE discord_id=?", (interaction.user.id,))
            row = await cur.fetchone()
            if row:
                my_score = int(row[0])
                cur = await db.execute("SELECT COUNT(*) FROM users WHERE mangul > ?", (my_score,))
                higher = (await cur.fetchone())[0]
                my_rank = int(higher) + 1
            else:
                my_rank, my_score = None, 0

        return build_rank_embed(
            guild=interaction.guild,
            top_rows=top_rows,
            my_rank=my_rank,
            my_score=my_score,
            page=1,
            page_size=PAGE_SIZE,
        )

    @discord.ui.button(label="새로고침 🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        embed = await self._build_embed(interaction)
        await interaction.followup.edit_message(message_id=interaction.message.id, embed=embed, view=self)


class DesignerEntryModal(discord.ui.Modal, title="디자이너 소통 공간 입장 신청"):
    활동명 = discord.ui.TextInput(
        label="활동명",
        placeholder="예: 프슈",
        required=True,
        max_length=50,
    )
    활동서버 = discord.ui.TextInput(
        label="활동하는 서버명",
        placeholder="예: 프슈 개발 일지",
        required=True,
        max_length=100,
    )
    경력 = discord.ui.TextInput(
        label="디자인 경력",
        placeholder="예: 배너·이모지·썸네일 1년",
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=400,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        member = interaction.user

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                    attach_files=True,
                    embed_links=True,
                )

        parent = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        channel_name = f"디자이너-신청-{member.id}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=parent if isinstance(parent, discord.CategoryChannel) else None,
            overwrites=overwrites,
            reason="디자이너 소통 공간 입장 신청 티켓 생성",
        )

        kst_now = (datetime.utcnow() + timedelta(hours=9)).isoformat(timespec="seconds")
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_designer_schema(db)
            await db.execute(
                """
                INSERT INTO designer_applications (
                    discord_id, display_name, activity_name, server_name, experience,
                    ticket_channel_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    member.id,
                    member.display_name,
                    str(self.활동명.value),
                    str(self.활동서버.value),
                    str(self.경력.value),
                    ticket_channel.id,
                    kst_now,
                ),
            )
            await db.commit()

        embed = discord.Embed(
            title="디자이너 소통 공간 입장 신청",
            description=(
                "작업물(데코, 이모지, 배너, 그림 등) 예시본을 파일로 첨부해주세요\n"
                "-# - 이모지, 배너, 데코 인증시 반드시 gif / png 파일로 올려주세요\n"
                "-# - 그림 인증시 해당 작업물의 레이어가 보이는 화면을 올려주세요\n\n"
                "- 관리자 확인까지 잠시만 대기해주세요 !\n"
                "- 관리자 확인에 따라 승인이 거부될 수 있습니다 !\n\n"
                "모든 문의 <@1327107014492557392> or <@772018925289996288> DM"
            ),
            color=discord.Color.from_rgb(255, 182, 193),
        )
        embed.add_field(name="활동명", value=str(self.활동명.value), inline=False)
        embed.add_field(name="활동하는 서버명", value=str(self.활동서버.value), inline=False)
        embed.add_field(name="디자인 경력", value=str(self.경력.value), inline=False)
        embed.set_footer(text="승인 또는 거부 버튼으로 결과를 처리해 주세요.")

        await ticket_channel.send(content=member.mention)
        msg = await ticket_channel.send(
            embed=embed,
            view=TicketModerationView(member_id=member.id, channel_id=ticket_channel.id, timeout=None),
        )

        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_designer_schema(db)
            await db.execute(
                "UPDATE designer_applications SET moderation_message_id=? WHERE ticket_channel_id=? AND status='pending'",
                (msg.id, ticket_channel.id),
            )
            await db.commit()

        await interaction.response.send_message(
            f"티켓이 생성되었습니다: {ticket_channel.mention}", ephemeral=True
        )

class TicketModerationView(discord.ui.View):
    def __init__(self, member_id: int, channel_id: int, *, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.member_id = member_id
        self.channel_id = channel_id

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _close_to_user(self, interaction: discord.Interaction):
        channel = interaction.guild.get_channel(self.channel_id)
        if channel is None:
            channel = await interaction.guild.fetch_channel(self.channel_id)
        assert isinstance(channel, discord.TextChannel)
        member = interaction.guild.get_member(self.member_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(self.member_id)
            except Exception:
                member = None
        if member is not None:
            await channel.set_permissions(member, view_channel=False, send_messages=False)
        return channel, member

    @discord.ui.button(label="승인", style=discord.ButtonStyle.success, custom_id="ticket_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction):
            await interaction.response.send_message(":x: 이 작업을 수행할 권한이 없습니다.", ephemeral=True)
            return
        channel, member = await self._close_to_user(interaction)

        kst_now = (datetime.utcnow() + timedelta(hours=9)).isoformat(timespec="seconds")
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_designer_schema(db)
            await db.execute(
                "UPDATE designer_applications SET status='approved', decided_at=?, decided_by=? WHERE ticket_channel_id=?",
                (kst_now, interaction.user.id, self.channel_id),
            )
            await db.commit()

        if DESIGNER_ROLE_ID and member is not None:
            role = interaction.guild.get_role(DESIGNER_ROLE_ID)
            if role is not None:
                try:
                    await member.add_roles(role, reason="디자이너 소통 공간 입장 승인")
                except Exception:
                    pass

        if member is not None:
            try:
                await member.send("안녕하세요. 디자이너 소통 공간 입장 신청이 **승인**되었습니다. 함께해 주셔서 감사합니다!")
            except Exception:
                pass

        await interaction.response.send_message("승인 처리되었습니다. 필요 시 채널을 정리해 주세요.", ephemeral=True)
        await interaction.message.edit(view=TicketDeleteView())

    @discord.ui.button(label="거부", style=discord.ButtonStyle.danger, custom_id="ticket_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction):
            await interaction.response.send_message(":x: 이 작업을 수행할 권한이 없습니다.", ephemeral=True)
            return
        channel, member = await self._close_to_user(interaction)

        kst_now = (datetime.utcnow() + timedelta(hours=9)).isoformat(timespec="seconds")
        async with aiosqlite.connect(DB_PATH) as db:
            await ensure_designer_schema(db)
            await db.execute(
                "UPDATE designer_applications SET status='rejected', decided_at=?, decided_by=? WHERE ticket_channel_id=?",
                (kst_now, interaction.user.id, self.channel_id),
            )
            await db.commit()

        if member is not None:
            try:
                await member.send("안녕하세요. 디자이너 소통 공간 입장 신청이 **거부**되었습니다. 문의 사항이 있으시면 알려 주세요.")
            except Exception:
                pass

        await interaction.response.send_message("거부 처리되었습니다. 필요 시 채널을 정리해 주세요.", ephemeral=True)
        await interaction.message.edit(view=TicketDeleteView())

class TicketDeleteView(discord.ui.View):
    @discord.ui.button(label="티켓 삭제하기", style=discord.ButtonStyle.danger, custom_id="ticket_delete")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_channels):
            await interaction.response.send_message(":x: 이 작업을 수행할 권한이 없습니다.", ephemeral=True)
            return
        try:
            await interaction.channel.delete(reason="디자이너 티켓 삭제")
        except Exception as e:
            await interaction.response.send_message(f":x: 채널 삭제 중 오류가 발생했습니다: {e}", ephemeral=True)
        else:
            try:
                await interaction.response.send_message("채널을 삭제했습니다.", ephemeral=True)
            except Exception:
                pass
            
_RPS_BEATS = {"가위": "보", "바위": "가위", "보": "바위"}
_RPS_ALL = ["가위", "바위", "보"]

def _rps_pick_bot_move(user_move: str) -> str:
    user_move = user_move.strip()
    if user_move not in _RPS_ALL:
        return random.choice(_RPS_ALL)
    bot_win = _RPS_BEATS[user_move]
    bot_tie = user_move
    for m in _RPS_ALL:
        if m != bot_win and m != bot_tie:
            bot_lose = m
            break
    return _weighted_choice(
        [bot_win, bot_tie, bot_lose],
        [RPS_BOT_WIN_PROB, RPS_TIE_PROB, 1.0 - (RPS_BOT_WIN_PROB + RPS_TIE_PROB)]
    )

def _rps_judge(user_move: str, bot_move: str) -> str:
    if user_move == bot_move:
        return "tie"
    return "win" if _RPS_BEATS[user_move] == bot_move else "lose"

_CHO_LIST = [
    "ㄱ", "ㄲ", "ㄴ", "ㄷ", "ㄸ", "ㄹ", "ㅁ", "ㅂ", "ㅃ",
    "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅉ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"
]

def _to_chosung(word: str) -> str:
    res = []
    for ch in word.strip():
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            sindex = code - 0xAC00
            cho = sindex // 588
            res.append(_CHO_LIST[cho])
        else:
            res.append(ch)
    return "".join(res)

def _is_korean_word(s: str) -> bool:
    return bool(re.fullmatch(r"[가-힣]+", s))

def _load_chosung_words() -> list[str]:
    global _cached_words
    if _cached_words is not None:
        return _cached_words
    words: list[str] = []
    try:
        with open(CHOSUNG_WORDS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if not w:
                    continue
                if _is_korean_word(w):
                    words.append(w)
    except FileNotFoundError:
        words = [
            "사과", "바나나", "딸기", "포도", "수박", "자두", "복숭아", "체리", "멜론", "오렌지",
            "하늘", "바다", "산책", "노을", "별빛", "강아지", "고양이", "라디오", "우산", "버스",
        ]
    _cached_words = words
    return words

class QuizAnswerModal(discord.ui.Modal, title="초성퀴즈 정답 입력"):
    def __init__(self, owner_id: int):
        super().__init__()
        self.owner_id = owner_id
        self.answer = discord.ui.TextInput(label="정답(한글)", placeholder="예: 바나나", required=True, max_length=30)
        self.add_item(self.answer)

    async def _disable_quiz_button(self, interaction: discord.Interaction):
        sess = _active_quiz_sessions.get(self.owner_id)
        if not sess:
            return
        msg_id = sess.get("message_id")
        ch_id = sess.get("channel_id")
        try:
            channel = interaction.client.get_channel(ch_id) if ch_id else interaction.channel
            if channel is None:
                return
            msg = await channel.fetch_message(msg_id)
            await msg.edit(view=QuizAnswerView(owner_id=self.owner_id, disabled=True))
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(":x: 이 문제는 다른 사용자의 세션입니다.", ephemeral=True)
            return
        sess = _active_quiz_sessions.get(self.owner_id)
        if not sess:
            await interaction.response.send_message(":x: 활성화된 문제를 찾을 수 없습니다.", ephemeral=True)
            return
        target = sess["word"]
        started = sess["start_ts"]
        now = datetime.utcnow().timestamp()

        if now - started > QUIZ_TIME_LIMIT_SECONDS:
            _active_quiz_sessions.pop(self.owner_id, None)
            await self._disable_quiz_button(interaction)
            await interaction.response.send_message(":alarm_clock: 시간 초과입니다!", ephemeral=True)
            return

        user_ans = str(self.answer.value).strip()
        if not _is_korean_word(user_ans):
            await interaction.response.send_message("❌ 한글 단어만 입력해 주세요.", ephemeral=True)
            return

        target_chosung = _to_chosung(target)
        user_chosung = _to_chosung(user_ans)

        ok = False
        if user_ans == target:
            ok = True
        elif user_chosung == target_chosung:
            async with aiosqlite.connect(DB_PATH) as db:
                await ensure_offline_dict_schema(db)
                ok = await is_in_offline_dictionary(db, user_ans)

        if ok:
            async with aiosqlite.connect(DB_PATH) as db:
                new_bal = await update_mangul(db, self.owner_id, QUIZ_REWARD)
                await db.commit()
            _active_quiz_sessions.pop(self.owner_id, None)
            await self._disable_quiz_button(interaction)
            await interaction.response.send_message(
                f"✅ 정답입니다! (입력: {user_ans}) **+{QUIZ_REWARD} 망울** 지급되었습니다.",
                ephemeral=True,
            )
        else:
            _active_quiz_sessions.pop(self.owner_id, None)
            await self._disable_quiz_button(interaction)
            await interaction.response.send_message("❌ 오답입니다! (오프라인 사전에 없음)", ephemeral=True)

class QuizAnswerView(discord.ui.View):
    def __init__(self, owner_id: int, *, disabled: bool = False):
        super().__init__(timeout=QUIZ_TIME_LIMIT_SECONDS)
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self._btn_answer = discord.ui.Button(label="정답 입력", style=discord.ButtonStyle.primary, disabled=disabled)
        self._btn_answer.callback = self._open_modal
        self.add_item(self._btn_answer)

    def bind_message(self, msg: discord.Message):
        self.message = msg

    async def _open_modal(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(":x: 이 문제는 다른 사용자의 세션입니다.", ephemeral=True)
            return
        if not _active_quiz_sessions.get(self.owner_id):
            try:
                for item in self.children:
                    if isinstance(item, discord.ui.Button):
                        item.disabled = True
                if self.message:
                    await self.message.edit(view=self)
            except Exception:
                pass
            await interaction.response.send_message(":white_check_mark: 이 문제는 이미 종료되었습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(QuizAnswerModal(owner_id=self.owner_id))

    async def on_timeout(self) -> None:
        try:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            if self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
        print("✅ 슬래시 명령어 전역 등록 완료")
    except Exception as e:
        print("❌ 동기화 실패:", e)

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_designer_schema(db)
        await ensure_quiz_schema(db)
        await ensure_offline_dict_schema(db)
        await db.commit()

        bot.add_view(TicketDeleteView(timeout=None))

        cur = await db.execute(
            "SELECT discord_id, ticket_channel_id, moderation_message_id FROM designer_applications WHERE status='pending' AND moderation_message_id IS NOT NULL"
        )
        rows = await cur.fetchall()

    for discord_id, channel_id, msg_id in rows:
        try:
            bot.add_view(
                TicketModerationView(member_id=int(discord_id), channel_id=int(channel_id), timeout=None),
                message_id=int(msg_id),
            )
        except Exception as e:
            print(f"[PersistentView] add_view 실패: msg={msg_id} err={e}")

    print(f"봇 로그인 성공: {bot.user}")

@bot.tree.command(name="망울순위", description="망울 보유량 랭킹 TOP 5를 보여줍니다.")
@app_commands.describe(page="보고 싶은 페이지 번호 (기본 1)")
@app_commands.checks.cooldown(1, 5.0)
async def cmd_rank(interaction: discord.Interaction, page: app_commands.Range[int, 1, 100000] = 1):
    await interaction.response.defer(ephemeral=False)

    PAGE_SIZE = 5
    page = max(1, int(page))
    offset = (page - 1) * PAGE_SIZE

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT discord_id, mangul
            FROM users
            ORDER BY mangul DESC
            LIMIT ? OFFSET ?
            """,
            (PAGE_SIZE, offset),
        )
        top_rows = await cur.fetchall()

        cur = await db.execute("SELECT mangul FROM users WHERE discord_id=?", (interaction.user.id,))
        row = await cur.fetchone()
        if row:
            my_score = int(row[0])
            cur = await db.execute("SELECT COUNT(*) FROM users WHERE mangul > ?", (my_score,))
            higher = (await cur.fetchone())[0]
            my_rank = int(higher) + 1
        else:
            my_rank, my_score = None, 0

    embed = build_rank_embed(
        guild=interaction.guild,
        top_rows=top_rows,
        my_rank=my_rank,
        my_score=my_score,
        page=page,
        page_size=PAGE_SIZE,
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="신청", description="디자이너 소통 공간 입장 신청을 시작합니다.")
async def create_designer_button(interaction: discord.Interaction):
    await interaction.response.send_modal(DesignerEntryModal())

@bot.tree.command(name="반반도박", description="50% 느낌의 2배 배당! (실제 승률 40%~49% 사이로 조정)")
@app_commands.describe(amount="베팅 금액")
async def cmd_coinflip(interaction: discord.Interaction, amount: app_commands.Range[int, 1, 1_000_000_000]):
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        balance = await _get_balance(db, interaction.user.id)
        if amount > balance:
            await interaction.followup.send(":x: 보유 망울이 부족합니다.")
            return
        win = random.random() < HOUSE_WIN_PROB_COIN
        delta = amount if win else -amount
        new_bal = await update_mangul(db, interaction.user.id, delta)
        await db.commit()

    if win:
        title = f"<a:1_4:1313925135434125356> 승리! +{amount} 망울"
        desc = f"배당 2배 적중! 현재 잔액: {new_bal}"
        color = discord.Color.from_rgb(255, 182, 193)
    else:
        title = f"<a:1_4:1313925135434125356> 패배… -{amount} 망울"
        desc = f"아쉽지만 다음 기회에! 현재 잔액: {new_bal}"
        color = discord.Color.dark_grey()

    embed = discord.Embed(title=title, description=desc, color=color)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="경마", description="3마 중 택1, 적중 시 3배 배당!")
@app_commands.describe(horse="배팅할 말 번호 (1~3)", amount="베팅 금액")
async def cmd_race(interaction: discord.Interaction, horse: app_commands.Range[int, 1, 3], amount: app_commands.Range[int, 1, 1_000_000_000]):
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        balance = await _get_balance(db, interaction.user.id)
        if amount > balance:
            await interaction.followup.send(":x: 보유 망울이 부족합니다.")
            return
        winner = random.choice([1, 2, 3])
        if winner == horse:
            delta = amount * 2
            result = "win"
        else:
            delta = -amount
            result = "lose"
        new_bal = await update_mangul(db, interaction.user.id, delta)
        await db.commit()

    if result == "win":
        title = f"<a:1_4:1313925135434125356> 적중! +{amount*2} 망울"
        desc = f"당첨 마: {winner}번 | 현재 잔액: {new_bal}"
        color = discord.Color.from_rgb(255, 182, 193)
    else:
        title = f"<a:1_4:1313925135434125356> 빗나감… -{amount} 망울"
        desc = f"당첨 마: {winner}번 | 현재 잔액: {new_bal}"
        color = discord.Color.dark_grey()

    embed = discord.Embed(title=title, description=desc, color=color)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="가위바위보", description="봇과 승부! 이기면 2배, 봇은 약간 유리합니다")
@app_commands.describe(move="가위/바위/보 중 하나", amount="베팅 금액")
@app_commands.choices(move=[
    app_commands.Choice(name="가위", value="가위"),
    app_commands.Choice(name="바위", value="바위"),
    app_commands.Choice(name="보", value="보"),
])
async def cmd_rps(interaction: discord.Interaction, move: app_commands.Choice[str], amount: app_commands.Range[int, 1, 1_000_000_000]):
    await interaction.response.defer(ephemeral=False)
    user_move = move.value

    async with aiosqlite.connect(DB_PATH) as db:
        balance = await _get_balance(db, interaction.user.id)
        if amount > balance:
            await interaction.followup.send(":x: 보유 망울이 부족합니다.")
            return
        bot_move = _rps_pick_bot_move(user_move)
        outcome = _rps_judge(user_move, bot_move)
        if outcome == "win":
            delta = amount
        elif outcome == "lose":
            delta = -amount
        else:
            delta = 0
        new_bal = await update_mangul(db, interaction.user.id, delta)
        await db.commit()

    if outcome == "win":
        title = f"<a:1_4:1313925135434125356> 승리! +{amount} 망울"
        color = discord.Color.from_rgb(255, 182, 193)
    elif outcome == "lose":
        title = f"<a:1_4:1313925135434125356> 패배… -{amount} 망울"
        color = discord.Color.dark_grey()
    else:
        title = f"<a:1_4:1313925135434125356> 비겼습니다! ±0"
        color = discord.Color.light_grey()

    desc = f"당신: **{user_move}** | 봇: **{bot_move}**\n현재 잔액: {new_bal}"
    embed = discord.Embed(title=title, description=desc, color=color)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="시간제한초성퀴즈", description="초성 힌트를 보고 15초 내 정답! 맞추면 15 망울 — 유저당 1분 쿨타임/일 5회")
async def cmd_chosung_quiz(interaction: discord.Interaction):
    kst_today = (datetime.utcnow() + timedelta(hours=9)).date().isoformat()
    now_ts = datetime.utcnow().timestamp()

    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_quiz_schema(db)
        cur = await db.execute(
            "SELECT used_count, last_used_ts FROM quiz_usage WHERE discord_id=? AND date=?",
            (interaction.user.id, kst_today),
        )
        row = await cur.fetchone()
        if row is None:
            used_count = 0
            last_used_ts = 0.0
            await db.execute(
                "INSERT INTO quiz_usage(discord_id, date, used_count, last_used_ts) VALUES (?, ?, 0, 0)",
                (interaction.user.id, kst_today),
            )
        else:
            used_count = int(row[0])
            last_used_ts = float(row[1])

        if used_count >= QUIZ_DAILY_LIMIT:
            await interaction.response.send_message(
                f":no_entry: 오늘 사용 횟수를 모두 소진하셨습니다. (일일 제한 {QUIZ_DAILY_LIMIT}회)",
                ephemeral=True,
            )
            return

        remain = int(QUIZ_CMD_COOLDOWN_SECONDS - (now_ts - last_used_ts))
        if remain > 0:
            await interaction.response.send_message(
                f":hourglass_flowing_sand: 잠시만요! {remain}초 뒤에 다시 시도해 주세요.",
                ephemeral=True,
            )
            return

        await db.execute(
            "UPDATE quiz_usage SET used_count=used_count+1, last_used_ts=? WHERE discord_id=? AND date=?",
            (now_ts, interaction.user.id, kst_today),
        )
        await db.commit()

    words = _load_chosung_words()
    target = random.choice(words)
    hint = _to_chosung(target)

    view = QuizAnswerView(owner_id=interaction.user.id)
    embed = discord.Embed(
        title="⏱️ 시간제한 초성퀴즈",
        description=(
            f"초성: **{hint}**\n"
            f"정답 제출은 버튼을 눌러 모달에 입력해 주세요.\n"
            f"제한 시간: **{QUIZ_TIME_LIMIT_SECONDS}초**, 보상: **{QUIZ_REWARD} 망울**\n"
            f"(명령어 쿨타임: 1분, 일일 제한: {QUIZ_DAILY_LIMIT}회)"
        ),
        color=discord.Color.from_rgb(255, 182, 193),
    )

    await interaction.response.send_message(embed=embed, view=view)
    try:
        msg = await interaction.original_response()
        view.bind_message(msg)
        _active_quiz_sessions[interaction.user.id] = {
            "word": target,
            "start_ts": now_ts,
            "message_id": msg.id,
            "channel_id": msg.channel.id,
        }
    except Exception:
        _active_quiz_sessions[interaction.user.id] = {
            "word": target,
            "start_ts": now_ts,
        }

@bot.tree.command(name="망울", description="내 망울(포인트)을 확인합니다.")
async def check_mangul(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        target = member or interaction.user
        bal = await get_or_create_user(db, target.id)

    embed = discord.Embed(
        title="<a:1_4:1313925135434125356> 망울 확인 !",
        description=f"{target.display_name} 님의 현재 망울은 {bal}개 입니다",
        color=discord.Color.teal()
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="망울부여", description="특정 유저에게 망울을 부여합니다.")
@app_commands.describe(member="대상 유저", amount="부여할 망울 수")
@app_commands.checks.has_permissions(manage_guild=True)
async def give_mangul(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message(":x: 0 이하의 값은 부여할 수 없습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        new_val = await update_mangul(db, member.id, amount)
    embed = discord.Embed(
        title="<a:1_4:1313925135434125356> 망울 부여 !",
        description=f"{member.display_name} 님에게 망울 {amount}개를 부여했습니다.\n현재 망울: {new_val}개",
        color=discord.Color.teal()
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="망울회수", description="특정 유저의 망울을 회수합니다.")
@app_commands.describe(member="대상 유저", amount="회수할 망울 수")
@app_commands.checks.has_permissions(manage_guild=True)
async def take_mangul(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message(":x: 0 이하의 값은 회수할 수 없습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        new_val = await update_mangul(db, member.id, -amount)
    embed = discord.Embed(
        title="<a:1_4:1313925135434125356> 망울 회수 !",
        description=f"{member.display_name} 님의 망울 {amount}개를 회수했습니다.\n현재 망울: {new_val}개",
        color=discord.Color.teal()
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="여우비", description="출석 체크하고 망울을 받습니다!")
async def attendance(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        now = (datetime.utcnow() + timedelta(hours=9)).date().isoformat()
        cur = await db.execute(
            "SELECT last_attendance, attendance_days FROM users WHERE discord_id=?",
            (interaction.user.id,)
        )
        row = await cur.fetchone()
        if row and row[0] == now:
            await interaction.followup.send(":x: 오늘은 이미 출석 체크를 하셨습니다!")
            return

        reward = 10
        new_val = await update_mangul(db, interaction.user.id, reward)

        if row:
            attendance_days = row[1] + 1
        else:
            attendance_days = 1

        await db.execute(
            "UPDATE users SET last_attendance=?, attendance_days=? WHERE discord_id=?",
            (now, attendance_days, interaction.user.id)
        )
        await db.commit()

    embed = discord.Embed(
        title=f"<a:1_4:1313925135434125356> {reward}개 망울 지급 완료",
        description=f"{interaction.user.display_name} 님, 총 {attendance_days}일 출석!",
        color=discord.Color.from_rgb(255, 182, 193)
    )
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="출석기록", description="출석 횟수 상위 5명을 확인합니다.")
async def attendance_ranking(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
        SELECT discord_id, attendance_days
        FROM users
        WHERE attendance_days > 0
        ORDER BY attendance_days DESC
        LIMIT 5
        """)
        rows = await cur.fetchall()
    if not rows:
        await interaction.followup.send("아직 출석 기록이 없습니다.")
        return
    description = ""
    for idx, (discord_id, days) in enumerate(rows, start=1):
        user = interaction.guild.get_member(discord_id) or bot.get_user(discord_id)
        name = getattr(user, "display_name", None) or f"탈퇴한 유저 ({discord_id})"
        description += f"**{idx}. {name}** — {days}일 출석\n"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT attendance_days FROM users WHERE discord_id=?", (interaction.user.id,))
        my_row = await cur.fetchone()
        my_days = my_row[0] if my_row else 0
        cur = await db.execute("SELECT COUNT(*)+1 FROM users WHERE attendance_days > ?", (my_days,))
        my_rank = (await cur.fetchone())[0]
    embed = discord.Embed(
        title="💗 출석 랭킹 Top 5",
        description=description + f"\n📌 {interaction.user.display_name} 님의 순위: {my_rank}위 ({my_days}일 출석)",
        color=discord.Color.from_rgb(255, 182, 193)
    )
    await interaction.followup.send(embed=embed)

@give_mangul.error
@take_mangul.error
async def admin_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(":no_entry: 관리자만 사용할 수 있습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f":warning: 오류: {error}", ephemeral=True)

if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    bot.run(TOKEN)
