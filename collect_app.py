import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

# ── 색상 테마 ──────────────────────────────────
BG        = "#0f0f1a"
SURFACE   = "#1a1a2e"
SURFACE2  = "#16213e"
CARD      = "#1e1e30"
TAB_ACT   = "#d4a017"
TAB_INACT = "#2a2a3d"
BTN_COLL  = "#cc6600"
BTN_ALL   = "#1a6b3a"
BTN_DEL   = "#3a3a4d"
BTN_UP    = "#7b2fa8"
BTN_BLUE  = "#1a4a8a"
BTN_SEL   = "#1a6b3a"
FG        = "#e0e0e0"
FG_DIM    = "#888899"
FG_YELLOW = "#f0c040"
FG_GREEN  = "#40c060"
FG_BLUE   = "#4090d0"
BORDER    = "#333355"
FONT_T    = ("맑은 고딕", 13, "bold")
FONT_B    = ("맑은 고딕", 9, "bold")
FONT_M    = ("맑은 고딕", 9)
FONT_S    = ("맑은 고딕", 8)

# ── 소스별 설정 ────────────────────────────────
SOURCES = {
    "블로그": {
        "label": "블로그 수집",
        "icon": "📝",
        "collect_cols": [
            ("네이버 블로그", FG_GREEN, ["인기글", "최신글", "이웃글"]),
            ("다음 블로그", FG_BLUE,   ["추천글", "최신글"]),
        ],
        "upload_cols": [("티스토리", FG_GREEN), ("워드프레스", FG_BLUE)],
    },
    "뉴스 통합": {
        "label": "뉴스 통합 수집 — 네이트·다음·네이버",
        "icon": "📰",
        "collect_cols": [
            ("네이트 연예 랭킹", FG_YELLOW, ["1위", "2위", "3위"]),
            ("다음 연예",        FG_BLUE,   ["연예", "스포츠", "이슈"]),
            ("네이버 엔터",      FG_GREEN,  ["연예", "스포츠", "뉴스"]),
        ],
        "upload_cols": [("네이버 블로그", FG_GREEN), ("티스토리", FG_BLUE)],
    },
    "카페": {
        "label": "카페 수집 — 네이버·다음 카페",
        "icon": "☕",
        "collect_cols": [
            ("네이버 카페", FG_GREEN, ["베스트글", "최신글", "인기글"]),
            ("다음 카페",   FG_BLUE,  ["추천글",  "최신글", "이슈"]),
        ],
        "upload_cols": [("네이버 카페", FG_GREEN), ("다음 카페", FG_BLUE)],
    },
    "인기글": {
        "label": "인기글 수집 — 각 플랫폼 인기 게시물",
        "icon": "🔥",
        "collect_cols": [
            ("네이버 인기글", FG_GREEN,  ["실시간", "일간", "주간"]),
            ("다음 인기글",   FG_BLUE,   ["실시간", "일간", "주간"]),
            ("DC 인기글",     FG_YELLOW, ["실시간", "일간", "주간"]),
        ],
        "upload_cols": [("네이버 블로그", FG_GREEN), ("티스토리", FG_BLUE)],
    },
    "유튜브": {
        "label": "유튜브 수집 — 트렌딩·채널·검색",
        "icon": "▶",
        "collect_cols": [
            ("트렌딩",  FG_YELLOW, ["인기",  "음악",  "게임"]),
            ("채널",    FG_GREEN,  ["구독",  "추천",  "최신"]),
            ("검색결과", FG_BLUE,  ["관련성", "최신", "조회수"]),
        ],
        "upload_cols": [("유튜브 설명글", FG_YELLOW), ("블로그 리뷰", FG_GREEN)],
    },
    "엑셀(대량)": {
        "label": "엑셀 대량 수집 — CSV·XLSX 파일 처리",
        "icon": "📊",
        "collect_cols": [
            ("엑셀 파일 A", FG_GREEN,  ["시트1", "시트2", "시트3"]),
            ("엑셀 파일 B", FG_BLUE,   ["시트1", "시트2", "시트3"]),
        ],
        "upload_cols": [("네이버 블로그", FG_GREEN), ("티스토리", FG_BLUE)],
    },
}

SOURCE_KEYS = list(SOURCES.keys())


def btn(parent, text, color, command=None, width=None, **kw):
    b = tk.Button(
        parent, text=text, bg=color, fg="white",
        activebackground=color, activeforeground="white",
        relief="flat", font=FONT_B, cursor="hand2",
        padx=6, pady=3, command=command or (lambda: None), **kw
    )
    if width:
        b.config(width=width)
    return b


class CollectPanel(tk.Frame):
    """수집 탭 패널 — 다중 컬럼 수집 소스"""

    def __init__(self, parent, source_key, **kw):
        super().__init__(parent, bg=BG, **kw)
        cfg = SOURCES[source_key]
        cols = cfg["collect_cols"]
        n = len(cols)

        # 상단 액션 바
        action_bar = tk.Frame(self, bg=BG)
        action_bar.pack(fill="x", padx=4, pady=(4, 6))

        btn(action_bar, f"⟳ {n}개 소스 전체 수집", BTN_COLL).pack(side="right", padx=2)
        btn(action_bar, "✓ 전체선택", BTN_SEL).pack(side="right", padx=2)
        btn(action_bar, "□ 전체해지", BTN_DEL).pack(side="right", padx=2)
        btn(action_bar, "⬆ 선택항목 업로드로 전송", BTN_UP).pack(side="right", padx=2)

        # 수집 기준일
        today = datetime.now().strftime("%Y년 %m월 %d일")
        tk.Label(action_bar, text=f"■ 수집 기준일: {today}",
                 bg=BG, fg=FG_DIM, font=FONT_S).pack(side="left", padx=2)

        # 컬럼 영역
        col_frame = tk.Frame(self, bg=BG)
        col_frame.pack(fill="both", expand=True, padx=4)
        for i in range(n):
            col_frame.columnconfigure(i, weight=1, uniform="col")

        for i, (col_name, col_color, sub_items) in enumerate(cols):
            self._build_column(col_frame, col_name, col_color, sub_items, i)

    def _build_column(self, parent, name, color, sub_items, col_idx):
        frame = tk.Frame(parent, bg=CARD, bd=1, relief="solid",
                         highlightbackground=BORDER, highlightthickness=1)
        frame.grid(row=0, column=col_idx, sticky="nsew", padx=3)

        # 컬럼 헤더
        hdr = tk.Frame(frame, bg=CARD)
        hdr.pack(fill="x", padx=4, pady=(4, 2))
        n_label = tk.Label(hdr, text=f"{col_idx+1}0{name}", bg=CARD, fg=color, font=FONT_B)
        n_label.pack(side="left")

        btn_bar = tk.Frame(frame, bg=CARD)
        btn_bar.pack(fill="x", padx=4, pady=(0, 4))
        btn(btn_bar, "✓ 전체", BTN_SEL, width=6).pack(side="left", padx=1)
        btn(btn_bar, "□ 해지",  BTN_DEL, width=6).pack(side="left", padx=1)
        btn(btn_bar, "🔍 수집", BTN_COLL, width=6).pack(side="left", padx=1)

        # 리스트 박스 (최소 높이 고정)
        listbox = tk.Listbox(
            frame, bg="#0a0a16", fg=FG, selectbackground=BTN_UP,
            relief="flat", font=FONT_S, height=8,
            activestyle="none", selectmode="extended"
        )
        listbox.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        for j, item in enumerate(sub_items):
            listbox.insert("end", f"  {item}")

        sb = tk.Scrollbar(frame, orient="vertical", command=listbox.yview,
                          bg=SURFACE, troughcolor=SURFACE, width=8)
        listbox.config(yscrollcommand=sb.set)


class UploadPanel(tk.Frame):
    """업로드 탭 패널"""

    def __init__(self, parent, source_key, **kw):
        super().__init__(parent, bg=BG, **kw)
        cfg = SOURCES[source_key]
        cols = cfg["upload_cols"]
        n = len(cols)

        # 상단 액션 바
        action_bar = tk.Frame(self, bg=BG)
        action_bar.pack(fill="x", padx=4, pady=(4, 6))

        btn(action_bar, "⬆ 선택항목 업로드", BTN_UP).pack(side="right", padx=2)
        btn(action_bar, "⟳ AI 재작성 후 업로드", BTN_COLL).pack(side="right", padx=2)
        btn(action_bar, "✓ 전체선택", BTN_SEL).pack(side="right", padx=2)
        btn(action_bar, "□ 전체해지", BTN_DEL).pack(side="right", padx=2)

        today = datetime.now().strftime("%Y년 %m월 %d일")
        tk.Label(action_bar, text=f"■ 업로드 기준일: {today}",
                 bg=BG, fg=FG_DIM, font=FONT_S).pack(side="left", padx=2)

        # 업로드 대상 컬럼
        col_frame = tk.Frame(self, bg=BG)
        col_frame.pack(fill="both", expand=True, padx=4)
        for i in range(n):
            col_frame.columnconfigure(i, weight=1, uniform="col")

        for i, (col_name, col_color) in enumerate(cols):
            self._build_upload_col(col_frame, col_name, col_color, i)

    def _build_upload_col(self, parent, name, color, col_idx):
        frame = tk.Frame(parent, bg=CARD, bd=1, relief="solid",
                         highlightbackground=BORDER, highlightthickness=1)
        frame.grid(row=0, column=col_idx, sticky="nsew", padx=3)

        hdr = tk.Frame(frame, bg=CARD)
        hdr.pack(fill="x", padx=4, pady=(4, 2))
        tk.Label(hdr, text=f"□ {name}", bg=CARD, fg=color, font=FONT_B).pack(side="left")

        btn_bar = tk.Frame(frame, bg=CARD)
        btn_bar.pack(fill="x", padx=4, pady=(0, 4))
        btn(btn_bar, "✓ 전체", BTN_SEL, width=6).pack(side="left", padx=1)
        btn(btn_bar, "□ 해지",  BTN_DEL, width=6).pack(side="left", padx=1)
        btn(btn_bar, "⬆ 업로드", BTN_UP, width=7).pack(side="left", padx=1)

        listbox = tk.Listbox(
            frame, bg="#0a0a16", fg=FG, selectbackground=BTN_UP,
            relief="flat", font=FONT_S, height=8,
            activestyle="none", selectmode="extended"
        )
        listbox.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        listbox.insert("end", "  (수집된 항목이 여기에 표시됩니다)")


class SourcePage(tk.Frame):
    """소스별 페이지 — 수집 / 업로드 서브탭 포함"""

    def __init__(self, parent, source_key, **kw):
        super().__init__(parent, bg=BG, **kw)
        cfg = SOURCES[source_key]

        # 페이지 제목
        title_bar = tk.Frame(self, bg=BG)
        title_bar.pack(fill="x", padx=6, pady=(4, 2))
        tk.Label(title_bar, text=f"■ {cfg['label']}",
                 bg=BG, fg=FG, font=FONT_B).pack(side="left")

        # 서브탭 (수집 / 업로드)
        tab_bar = tk.Frame(self, bg=BG)
        tab_bar.pack(fill="x", padx=6, pady=(0, 4))

        self._panels = {}
        self._tab_btns = {}

        panel_host = tk.Frame(self, bg=BG)
        panel_host.pack(fill="both", expand=True)

        for name, PanelCls in [("수집", CollectPanel), ("업로드", UploadPanel)]:
            panel = PanelCls(panel_host, source_key)
            self._panels[name] = panel

        for name in ["수집", "업로드"]:
            b = tk.Button(
                tab_bar, text=f"  {name}  ",
                bg=TAB_INACT, fg=FG, activebackground=SURFACE2,
                relief="flat", font=FONT_M, cursor="hand2",
                command=lambda n=name: self._switch(n)
            )
            b.pack(side="left", padx=1)
            self._tab_btns[name] = b

        self._switch("수집")

    def _switch(self, name):
        for n, panel in self._panels.items():
            panel.pack_forget()
        self._panels[name].pack(fill="both", expand=True)
        for n, b in self._tab_btns.items():
            b.config(bg=SURFACE2 if n == name else TAB_INACT,
                     fg=FG_YELLOW if n == name else FG)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("통합 수집/발행 페이지")
        # 한 화면에 맞도록 고정 크기 (스크롤 없음)
        self.geometry("1200x680")
        self.minsize(1100, 640)
        self.configure(bg=BG)
        self._build_ui()

    def _build_ui(self):
        # ── 상단 헤더 ──────────────────────────
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(header, text="🔧 통합 수집/발행 페이지",
                 bg=BG, fg=FG_YELLOW, font=FONT_T).pack(side="left")
        tk.Label(header,
                 text="위에서 소스를 선택하면 해당 페이지가 아래에 표시됩니다. "
                      "기존 페이지의 모든 기능(수집,AI 재작성,이미지 생성,업로드)이 그대로 동작합니다.",
                 bg=BG, fg=FG_DIM, font=FONT_S).pack(side="left", padx=10)

        # ── 소스 탭 바 ─────────────────────────
        src_label = tk.Label(self, text="  🔌 수집 소스",
                             bg=BG, fg=FG_DIM, font=FONT_S)
        src_label.pack(anchor="w", padx=10, pady=(4, 1))

        tab_bar = tk.Frame(self, bg=BG)
        tab_bar.pack(fill="x", padx=10, pady=(0, 4))

        self._src_btns = {}
        self._src_pages = {}

        page_host = tk.Frame(self, bg=BG, bd=1, relief="solid",
                             highlightbackground=BORDER, highlightthickness=1)
        page_host.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        ICONS = {"블로그": "📝", "뉴스 통합": "📰", "카페": "☕",
                 "인기글": "🔥", "유튜브": "▶", "엑셀(대량)": "📊"}

        for key in SOURCE_KEYS:
            page = SourcePage(page_host, key)
            self._src_pages[key] = page

            b = tk.Button(
                tab_bar, text=f" {ICONS[key]} {key} ",
                bg=TAB_INACT, fg=FG,
                activebackground=SURFACE2, activeforeground=FG_YELLOW,
                relief="flat", font=FONT_B, cursor="hand2",
                padx=6, pady=5,
                command=lambda k=key: self._select_source(k)
            )
            b.pack(side="left", padx=1)
            self._src_btns[key] = b

        self._select_source("뉴스 통합")

    def _select_source(self, key):
        for k, page in self._src_pages.items():
            page.pack_forget()
        self._src_pages[key].pack(fill="both", expand=True)
        for k, b in self._src_btns.items():
            b.config(
                bg=TAB_ACT if k == key else TAB_INACT,
                fg="#1a1a1a" if k == key else FG
            )


if __name__ == "__main__":
    app = App()
    app.mainloop()
