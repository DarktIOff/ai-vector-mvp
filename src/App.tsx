import {
  AlertCircle,
  Archive,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  BarChart3,
  Bell,
  BookOpen,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  ClipboardCheck,
  Clock3,
  Database,
  Download,
  FileBarChart,
  FileCheck2,
  FilePlus2,
  FileText,
  Filter,
  Info,
  LayoutPanelLeft,
  Library,
  ListFilter,
  Menu,
  MessageSquarePlus,
  Moon,
  MoreHorizontal,
  Paperclip,
  PanelLeftClose,
  PanelRightClose,
  Pin,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Send,
  Settings,
  ShieldCheck,
  Sparkles,
  Square,
  Sun,
  Table2,
  Tag,
  Trash2,
  Upload,
  UserRound,
  WandSparkles,
  X,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  ANALYTIC_AGENT_V2,
  apiClient,
  backendApi,
  DEMO_MODE,
  type AiStatus,
  type AnalysisProject,
  type AnalysisSynthesisResult,
  type AnalysisSection,
  type ClarificationOption,
  type ConversationDetail,
  type ConversationSummary,
  type DataScanResult,
  type ImportResult,
  type QueryFact,
  type QueryPlan,
  type QueryResult,
  type QuerySeries,
  type ReportChartBlock,
  type ReportDocument,
  type ReportGenerateParameters,
  type ReportMode,
  type ReportParameterCatalog,
  type ResolvedMetric,
  type Scenario,
} from "./api";
import {
  chartData,
  chats,
  metrics,
  okvedRows,
  processingLabels,
  revenueRows,
  sources,
  tags,
  templates,
} from "./data";
import type {
  DataContext,
  Metric,
  ProcessingStep,
  QueryMode,
  ReviewStatus,
  RevenueRow,
  Role,
  Theme,
  Workspace,
} from "./types";

const roles: Role[] = [
  "Пользователь отчётности",
  "Аналитик",
  "Проверяющий",
  "Методолог",
  "Владелец шаблонов",
  "Администратор",
  "Аудитор",
];

const defaultContext: DataContext = {
  form: "1-МСП",
  territory: "Российская Федерация",
  reportingPeriod: "Январь–июнь 2026",
  comparisonPeriod: "Январь–июнь 2025",
  dataVersion: "Контрольная пара 2025–2026",
};

export function isStandardReportCommand(value: string): boolean {
  const normalized = value
    .trim()
    .toLocaleLowerCase("ru-RU")
    .replace(/ё/g, "е")
    .replace(/[‐‑‒–—]/g, "-")
    .replace(/[«»"'.,!?;:()]/g, " ")
    .replace(/\s+/g, " ");
  const hasCommandPrefix =
    /^(?:пожалуйста\s+)?(?:собери|сформируй|подготовь|сделай)(?:\s+мне)?\s+(?:стандартн(?:ый|ую)\s+)?(?:отчет|справку)(?:\s|$)/.test(
      normalized,
    );
  if (!hasCommandPrefix) return false;
  return (
    /(?:^|\s)стандартн(?:ый|ую)(?:\s|$)/.test(normalized) ||
    /(?:^|\s)1\s*-\s*мсп(?:\s|$)/.test(normalized) ||
    /(?:^|\s)по\s+(?:региону|стране|россии|рф|российской\s+федерации)(?:\s|$)/.test(
      normalized,
    )
  );
}

const statusLabels: Record<ReviewStatus, string> = {
  draft: "Черновик",
  processing: "Выполняется",
  needs_review: "Требует проверки",
  revision_requested: "На доработке",
  approved: "Утверждён",
};

const signalLabels: Record<Metric["signal"], string> = {
  normal: "В норме",
  attention: "Внимание",
  adverse: "Неблагоприятно",
  unknown: "Нет оценки",
};

function readStored<T>(key: string, fallback: T): T {
  try {
    const value = localStorage.getItem(key);
    return value ? (JSON.parse(value) as T) : fallback;
  } catch {
    return fallback;
  }
}

export function App() {
  const [workspace, setWorkspace] = useState<Workspace>("home");
  const [theme, setTheme] = useState<Theme>(() =>
    readStored<Theme>("ai-vector-theme", "system"),
  );
  const [leftCollapsed, setLeftCollapsed] = useState(() =>
    readStored("ai-vector-left-collapsed", false),
  );
  const [rightCollapsed, setRightCollapsed] = useState(() =>
    readStored("ai-vector-right-collapsed", false),
  );
  const [mobileLeftOpen, setMobileLeftOpen] = useState(false);
  const [mobileRightOpen, setMobileRightOpen] = useState(false);
  const [role, setRole] = useState<Role>("Аналитик");
  const [context, setContext] = useState<DataContext>(defaultContext);
  const [inspectorTab, setInspectorTab] = useState<
    "parameters" | "sources" | "tags" | "warnings"
  >("parameters");
  const [reportStatus, setReportStatus] = useState<ReviewStatus>("draft");
  const [toast, setToast] = useState("");
  const [queryResult, setQueryResult] = useState<QueryResult | null>(null);
  const [conversationList, setConversationList] = useState<
    ConversationSummary[]
  >([]);
  const [activeConversationId, setActiveConversationId] = useState<
    string | null
  >(() => localStorage.getItem("ai-vector-active-conversation"));
  const [analysisProject, setAnalysisProject] =
    useState<AnalysisProject | null>(null);
  const [projectLoading, setProjectLoading] = useState(false);
  const [projectLoadingStage, setProjectLoadingStage] = useState(
    "Собираем добавленные ответы и таблицы",
  );
  const [scenarioList, setScenarioList] = useState<Scenario[]>([]);
  const [pendingReportCommand, setPendingReportCommand] = useState<
    string | null
  >(null);

  useEffect(() => {
    try {
      localStorage.setItem("ai-vector-theme", JSON.stringify(theme));
    } catch {
      // Persistence can be unavailable in private or restricted browser contexts.
    }
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const applyTheme = () => {
      document.documentElement.dataset.theme =
        theme === "system" ? (media.matches ? "dark" : "light") : theme;
    };
    applyTheme();
    media.addEventListener("change", applyTheme);
    return () => media.removeEventListener("change", applyTheme);
  }, [theme]);

  useEffect(() => {
    try {
      localStorage.setItem(
        "ai-vector-left-collapsed",
        JSON.stringify(leftCollapsed),
      );
      localStorage.setItem(
        "ai-vector-right-collapsed",
        JSON.stringify(rightCollapsed),
      );
    } catch {
      // The application remains usable when storage is disabled.
    }
  }, [leftCollapsed, rightCollapsed]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 3200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const refreshConversations = async () => {
    if (DEMO_MODE) return;
    try {
      const [loadedConversations, loadedScenarios] = await Promise.all([
        backendApi.conversations(),
        backendApi.scenarios(),
      ]);
      setConversationList(loadedConversations);
      setScenarioList(loadedScenarios);
    } catch {
      setConversationList([]);
      setScenarioList([]);
    }
  };

  useEffect(() => {
    void refreshConversations();
  }, []);

  const navigate = (next: Workspace) => {
    setWorkspace(next);
    setMobileLeftOpen(false);
  };

  const openStandardReportCommand = (command: string) => {
    setPendingReportCommand(command);
    navigate("generator");
  };

  const createConversation = async (initialQuery?: string) => {
    if (DEMO_MODE) {
      if (initialQuery) sessionStorage.setItem("ai-vector-query", initialQuery);
      navigate("analysis");
      return;
    }
    try {
      const conversation = await backendApi.createConversation();
      setActiveConversationId(conversation.id);
      localStorage.setItem("ai-vector-active-conversation", conversation.id);
      if (initialQuery) {
        sessionStorage.setItem("ai-vector-query", initialQuery);
      }
      await refreshConversations();
      navigate("analysis");
    } catch (error) {
      setToast(
        error instanceof Error ? error.message : "Не удалось создать диалог",
      );
    }
  };

  const openConversation = (conversationId: string) => {
    setActiveConversationId(conversationId);
    localStorage.setItem("ai-vector-active-conversation", conversationId);
    navigate("analysis");
  };

  const toggleConversationPin = async (
    conversation: ConversationSummary,
  ) => {
    try {
      await backendApi.updateConversation(conversation.id, {
        pinned: !conversation.pinned,
      });
      await refreshConversations();
    } catch (error) {
      setToast(
        error instanceof Error ? error.message : "Не удалось изменить закрепление",
      );
    }
  };

  const deleteConversation = async (
    conversation: ConversationSummary,
  ) => {
    if (conversation.status === "processing") {
      setToast("Дождитесь завершения анализа перед удалением диалога");
      return;
    }
    const confirmed = window.confirm(
      `Удалить диалог «${conversation.title}» без возможности восстановления?\n\nСвязанные ходы и проект также будут удалены.`,
    );
    if (!confirmed) return;
    try {
      await backendApi.deleteConversation(conversation.id);
      const remaining = conversationList.filter(
        (item) => item.id !== conversation.id,
      );
      setConversationList(remaining);

      const deletingActive = activeConversationId === conversation.id;
      const deletingOpenProject =
        analysisProject?.conversation_id === conversation.id;
      if (deletingActive) {
        const nextConversationId = remaining[0]?.id || null;
        setActiveConversationId(nextConversationId);
        if (nextConversationId) {
          localStorage.setItem(
            "ai-vector-active-conversation",
            nextConversationId,
          );
        } else {
          localStorage.removeItem("ai-vector-active-conversation");
        }
        if (workspace === "analysis" && !nextConversationId) {
          navigate("home");
        }
      }
      if (deletingOpenProject) {
        setAnalysisProject(null);
        navigate(remaining.length > 0 ? "analysis" : "home");
      }
      await refreshConversations();
      setToast("Диалог удалён");
    } catch (error) {
      setToast(
        error instanceof Error ? error.message : "Не удалось удалить диалог",
      );
    }
  };

  const openAnalysisProject = async (conversationId: string) => {
    setAnalysisProject(null);
    setProjectLoadingStage("Собираем добавленные ответы и таблицы");
    setProjectLoading(true);
    navigate("project");
    try {
      let project = await backendApi.conversationProject(conversationId);
      if (
        project.blocks_count > 0 &&
        (
          project.document.presentation_version !== 5 ||
          !project.document.synthesis_mode
        )
      ) {
        setProjectLoadingStage(
          "Проектируем структуру и заголовок отчёта",
        );
        const generated = await backendApi.synthesizeAnalysisProject(
          project.id,
        );
        project = generated.project;
        setToast(
          generated.used_ai
            ? "Отчёт отредактирован в GNIVC LOCAL по всей беседе и проверенным данным"
            : "ИИ недоступен: сформирован проверенный базовый отчёт",
        );
      }
      setAnalysisProject(project);
    } catch (error) {
      setToast(
        error instanceof Error ? error.message : "Не удалось открыть проект",
      );
      navigate("analysis");
    } finally {
      setProjectLoading(false);
    }
  };

  const runScenario = async (scenarioId: string) => {
    try {
      const run = await backendApi.runScenario(scenarioId);
      setActiveConversationId(run.conversation.id);
      localStorage.setItem(
        "ai-vector-active-conversation",
        run.conversation.id,
      );
      await refreshConversations();
      navigate("analysis");
      const last = run.results.at(-1);
      setToast(
        last?.status === "ready"
          ? "Сценарий выполнен в новом диалоге"
          : "Сценарий остановлен на уточнении",
      );
    } catch (error) {
      setToast(
        error instanceof Error ? error.message : "Не удалось запустить сценарий",
      );
    }
  };

  const showInspector = (
    tab: "parameters" | "sources" | "tags" | "warnings",
  ) => {
    setInspectorTab(tab);
    setRightCollapsed(false);
    setMobileRightOpen(true);
  };

  return (
    <div className="app-shell">
      <TopBar
        theme={theme}
        setTheme={setTheme}
        role={role}
        setRole={setRole}
        onHome={() => navigate("home")}
        onOpenLeft={() => setMobileLeftOpen(true)}
        onOpenRight={() => setMobileRightOpen(true)}
      />

      <div
        className={`app-body ${leftCollapsed ? "left-collapsed" : ""} ${
          rightCollapsed ? "right-collapsed" : ""
        }`}
      >
        <LeftSidebar
          collapsed={leftCollapsed}
          mobileOpen={mobileLeftOpen}
          workspace={workspace}
          onCloseMobile={() => setMobileLeftOpen(false)}
          onToggle={() => setLeftCollapsed((value) => !value)}
          onNavigate={navigate}
          conversations={conversationList}
          activeConversationId={activeConversationId}
          onNewConversation={() => void createConversation()}
          onOpenConversation={openConversation}
          onTogglePin={(conversation) =>
            void toggleConversationPin(conversation)
          }
          onDeleteConversation={(conversation) =>
            void deleteConversation(conversation)
          }
          scenarios={scenarioList}
          onRunScenario={(scenarioId) => void runScenario(scenarioId)}
        />

        <main className="main-area" id="main-content">
          {workspace === "home" && (
            <HomeWorkspace
              context={context}
              onNavigate={navigate}
              onShowInspector={showInspector}
              onStartConversation={(query) => {
                if (!DEMO_MODE && isStandardReportCommand(query)) {
                  openStandardReportCommand(query);
                  return;
                }
                void createConversation(query);
              }}
            />
          )}
          {workspace === "analysis" && (
            <AnalysisWorkspace
              key={activeConversationId || "legacy-analysis"}
              activeConversationId={activeConversationId || undefined}
              onNavigate={navigate}
              onShowInspector={showInspector}
              onToast={setToast}
              onResult={setQueryResult}
              onConversationChanged={() => void refreshConversations()}
              onOpenProject={(conversationId) =>
                void openAnalysisProject(conversationId)
              }
              onOpenStandardReport={openStandardReportCommand}
            />
          )}
          {workspace === "data" && <DataWorkspace onToast={setToast} />}
          {workspace === "generator" && (
            <GeneratorWorkspace
              context={context}
              onContextChange={setContext}
              onOpenReport={() => {
                setReportStatus("draft");
                navigate("report");
              }}
              initialCommand={pendingReportCommand || undefined}
              onInitialCommandConsumed={() => setPendingReportCommand(null)}
            />
          )}
          {workspace === "report" && (
            DEMO_MODE ? (
              <ReportWorkspace
                context={context}
                role={role}
                status={reportStatus}
                setStatus={setReportStatus}
                onBack={() => navigate("analysis")}
                onShowInspector={showInspector}
                onToast={setToast}
              />
            ) : (
              <LiveReportWorkspace
                status={reportStatus}
                setStatus={setReportStatus}
                role={role}
                onBack={() => navigate("analysis")}
                onToast={setToast}
              />
            )
          )}
          {workspace === "project" && activeConversationId && (
            projectLoading ? (
              <ReportLoadingScreen stage={projectLoadingStage} />
            ) : (
              <AnalysisProjectWorkspace
                conversationId={activeConversationId}
                initialProject={analysisProject}
                onBack={() => navigate("analysis")}
                onToast={setToast}
              />
            )
          )}
          {workspace === "templates" && (
            <TemplateWorkspace onToast={setToast} />
          )}
          {workspace === "knowledge" && (
            <KnowledgeWorkspace onToast={setToast} />
          )}
          {workspace === "jobs" && <JobsWorkspace onToast={setToast} />}
        </main>

        <RightInspector
          collapsed={rightCollapsed}
          mobileOpen={mobileRightOpen}
          activeTab={inspectorTab}
          context={context}
          setContext={setContext}
          onCloseMobile={() => setMobileRightOpen(false)}
          onToggle={() => setRightCollapsed((value) => !value)}
          onTabChange={setInspectorTab}
          queryResult={queryResult}
        />
      </div>

      {(mobileLeftOpen || mobileRightOpen) && (
        <button
          className="mobile-backdrop"
          aria-label="Закрыть панель"
          onClick={() => {
            setMobileLeftOpen(false);
            setMobileRightOpen(false);
          }}
        />
      )}

      {toast && (
        <div className="toast" role="status">
          <CheckCircle2 size={18} />
          {toast}
        </div>
      )}
    </div>
  );
}

interface TopBarProps {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  role: Role;
  setRole: (role: Role) => void;
  onHome: () => void;
  onOpenLeft: () => void;
  onOpenRight: () => void;
}

function TopBar({
  theme,
  setTheme,
  role,
  setRole,
  onHome,
  onOpenLeft,
  onOpenRight,
}: TopBarProps) {
  const [userOpen, setUserOpen] = useState(false);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);

  useEffect(() => {
    if (DEMO_MODE) return;
    const controller = new AbortController();
    backendApi
      .aiStatus(controller.signal)
      .then(setAiStatus)
      .catch(() => setAiStatus(null));
    return () => controller.abort();
  }, []);

  const cycleTheme = () => {
    const sequence: Theme[] = ["system", "light", "dark"];
    setTheme(sequence[(sequence.indexOf(theme) + 1) % sequence.length]);
  };

  return (
    <header className="topbar">
      <div className="brand-area">
        <button className="icon-button mobile-only" onClick={onOpenLeft}>
          <Menu size={20} />
          <span className="sr-only">Открыть навигацию</span>
        </button>
        <button
          type="button"
          className="brand"
          aria-label="Перейти на главную страницу ИИ-ВЕКТОР"
          onClick={onHome}
        >
          <span className="brand-mark">
            <CircleDot size={17} />
          </span>
          <span>ИИ-ВЕКТОР</span>
        </button>
      </div>

      <div className="top-actions">
        <span
          className={`freshness ${aiStatus ? "" : "offline"}`}
          title={
            aiStatus
              ? `GNIVC LOCAL; редактор отчёта: ${aiStatus.report_synthesis_status === "configured" ? "готов" : "не настроен"}; политика данных: ${aiStatus.data_policy}`
              : DEMO_MODE
                ? "Явно включён демонстрационный режим"
                : "Запустите backend на порту 8000"
          }
        >
          {aiStatus || DEMO_MODE ? (
            <CheckCircle2 size={15} />
          ) : (
            <AlertCircle size={15} />
          )}
          {DEMO_MODE
            ? "Демо-режим"
            : aiStatus
              ? "ИИ: GNIVC LOCAL"
              : "Backend недоступен"}
        </span>
        <button
          className="icon-button"
          onClick={cycleTheme}
          title={`Тема: ${theme === "system" ? "системная" : theme === "light" ? "светлая" : "тёмная"}`}
        >
          {theme === "dark" ? (
            <Moon size={18} />
          ) : theme === "light" ? (
            <Sun size={18} />
          ) : (
            <LayoutPanelLeft size={18} />
          )}
          <span className="sr-only">Сменить тему</span>
        </button>
        <button className="icon-button notification-button">
          <Bell size={18} />
          <span />
          <span className="sr-only">Уведомления</span>
        </button>
        <button
          className="icon-button mobile-only"
          onClick={onOpenRight}
          aria-label="Открыть параметры"
        >
          <Settings size={19} />
        </button>
        <div className="user-menu-wrap">
          <button
            className="user-button"
            aria-expanded={userOpen}
            onClick={() => setUserOpen((value) => !value)}
          >
            <span className="avatar">ГМ</span>
            <span className="user-copy">
              <strong>Геворг М.</strong>
              <small>{role}</small>
            </span>
            <ChevronDown size={14} />
          </button>
          {userOpen && (
            <div className="popover role-popover">
              <div className="popover-title">Демонстрационная роль</div>
              {roles.map((item) => (
                <button
                  key={item}
                  className={role === item ? "selected" : ""}
                  onClick={() => {
                    setRole(item);
                    setUserOpen(false);
                  }}
                >
                  <span>{item}</span>
                  {role === item && <Check size={15} />}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </header>
  );
}

interface LeftSidebarProps {
  collapsed: boolean;
  mobileOpen: boolean;
  workspace: Workspace;
  onCloseMobile: () => void;
  onToggle: () => void;
  onNavigate: (workspace: Workspace) => void;
  conversations: ConversationSummary[];
  activeConversationId: string | null;
  onNewConversation: () => void;
  onOpenConversation: (conversationId: string) => void;
  onTogglePin: (conversation: ConversationSummary) => void;
  onDeleteConversation: (conversation: ConversationSummary) => void;
  scenarios: Scenario[];
  onRunScenario: (scenarioId: string) => void;
}

function LeftSidebar({
  collapsed,
  mobileOpen,
  workspace,
  onCloseMobile,
  onToggle,
  onNavigate,
  conversations,
  activeConversationId,
  onNewConversation,
  onOpenConversation,
  onTogglePin,
  onDeleteConversation,
  scenarios,
  onRunScenario,
}: LeftSidebarProps) {
  const [section, setSection] = useState<
    "chats" | "scenarios" | "templates"
  >("chats");
  const [search, setSearch] = useState("");
  const workspaceChats: ConversationSummary[] = DEMO_MODE
    ? chats.map((chat) => ({
        id: chat.id,
        title: chat.title,
        context: chat.context,
        status: chat.status,
        pinned: chat.pinned,
        archived: false,
        created_at: "",
        updated_at: chat.updatedAt,
        project_id: null,
        turns_count: 1,
      }))
    : conversations;
  const filteredChats = workspaceChats.filter((chat) =>
    chat.title.toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <aside
      className={`left-sidebar ${collapsed ? "collapsed" : ""} ${
        mobileOpen ? "mobile-open" : ""
      }`}
      aria-label="Основная навигация"
    >
      <div className="sidebar-top">
        {!collapsed && <span className="sidebar-label">Рабочее пространство</span>}
        <button
          className="icon-button desktop-toggle"
          onClick={onToggle}
          title={collapsed ? "Развернуть панель" : "Свернуть панель"}
        >
          {collapsed ? (
            <ChevronRight size={18} />
          ) : (
            <PanelLeftClose size={18} />
          )}
        </button>
        <button className="icon-button mobile-only" onClick={onCloseMobile}>
          <X size={20} />
          <span className="sr-only">Закрыть навигацию</span>
        </button>
      </div>

      <button
        className="new-chat-button"
        onClick={onNewConversation}
        title="Новый диалог"
      >
        <MessageSquarePlus size={18} />
        {!collapsed && <span>Новый диалог</span>}
      </button>

      {!collapsed && (
        <div className="sidebar-tabs" role="tablist">
          <button
            className={section === "chats" ? "active" : ""}
            onClick={() => setSection("chats")}
          >
            Диалоги
          </button>
          <button
            className={section === "scenarios" ? "active" : ""}
            onClick={() => setSection("scenarios")}
          >
            Сценарии
          </button>
          <button
            className={section === "templates" ? "active" : ""}
            onClick={() => setSection("templates")}
          >
            Шаблоны
          </button>
        </div>
      )}

      <div className="sidebar-content">
        {!collapsed && section === "chats" && (
          <>
            <div className="search-field compact-field">
              <Search size={15} />
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Поиск по диалогам"
                aria-label="Поиск по диалогам"
              />
              <kbd>⌘K</kbd>
            </div>
            <div className="sidebar-group-label">
              <span>Закреплённые</span>
            </div>
            {filteredChats
              .filter((chat) => chat.pinned)
              .map((chat) => (
                <ChatItem
                  key={chat.id}
                  chat={chat}
                  active={
                    workspace === "analysis" &&
                    (DEMO_MODE
                      ? chat.id === "c2"
                      : chat.id === activeConversationId)
                  }
                  onClick={() =>
                    DEMO_MODE
                      ? onNavigate("analysis")
                      : onOpenConversation(chat.id)
                  }
                  onTogglePin={() => onTogglePin(chat)}
                  onDelete={() => onDeleteConversation(chat)}
                />
              ))}
            <div className="sidebar-group-label">
              <span>Недавние</span>
            </div>
            {filteredChats
              .filter((chat) => !chat.pinned)
              .map((chat) => (
                <ChatItem
                  key={chat.id}
                  chat={chat}
                  active={
                    workspace === "analysis" &&
                    chat.id === activeConversationId
                  }
                  onClick={() =>
                    DEMO_MODE
                      ? onNavigate(chat.id === "c1" ? "report" : "analysis")
                      : onOpenConversation(chat.id)
                  }
                  onTogglePin={() => onTogglePin(chat)}
                  onDelete={() => onDeleteConversation(chat)}
                />
              ))}
          </>
        )}

        {!collapsed && section === "templates" && (
          <div className="template-mini-list">
            <button
              className="outline-button wide"
              onClick={() => onNavigate("templates")}
            >
              <Upload size={16} />
              Загрузить Word
            </button>
            {templates.slice(0, 5).map((template) => (
              <button
                className="template-mini"
                key={template.id}
                onClick={() =>
                  onNavigate(template.id === "t1" ? "generator" : "templates")
                }
              >
                <span className="file-icon">
                  <FileText size={16} />
                </span>
                <span>
                  <strong>{template.name}</strong>
                  <small>
                    v{template.version} ·{" "}
                    {template.status === "approved"
                      ? "Утверждён"
                      : "Черновик"}
                  </small>
                </span>
              </button>
            ))}
            <button
              className="text-button"
              onClick={() => onNavigate("templates")}
            >
              Все шаблоны <ArrowRight size={14} />
            </button>
          </div>
        )}

        {!collapsed && section === "scenarios" && (
          <div className="template-mini-list scenario-mini-list">
            {scenarios.length === 0 && (
              <p className="sidebar-empty-copy">
                Сохраните последовательность вопросов кнопкой «Сохранить
                сценарий».
              </p>
            )}
            {scenarios.map((scenario) => (
              <button
                className="template-mini"
                key={scenario.id}
                onClick={() => onRunScenario(scenario.id)}
              >
                <span className="file-icon">
                  <Play size={15} />
                </span>
                <span>
                  <strong>{scenario.title}</strong>
                  <small>{scenario.steps.length} шагов · запустить в новом чате</small>
                </span>
              </button>
            ))}
          </div>
        )}

        {collapsed && (
          <nav className="collapsed-nav">
            <button
              className={workspace === "home" ? "active" : ""}
              onClick={() => onNavigate("home")}
              title="Диалоги"
            >
              <MessageSquarePlus size={19} />
            </button>
            <button
              className={workspace === "templates" ? "active" : ""}
              onClick={() => onNavigate("templates")}
              title="Шаблоны"
            >
              <FileText size={19} />
            </button>
            <button
              className={workspace === "generator" ? "active" : ""}
              onClick={() => onNavigate("generator")}
              title="Отчёт 1-МСП"
            >
              <FileBarChart size={19} />
            </button>
            <button
              className={workspace === "data" ? "active" : ""}
              onClick={() => onNavigate("data")}
              title="Данные"
            >
              <Database size={19} />
            </button>
          </nav>
        )}
      </div>

      <nav className="sidebar-bottom">
        <NavButton
          collapsed={collapsed}
          icon={<Database size={17} />}
          label="Данные"
          active={workspace === "data"}
          onClick={() => onNavigate("data")}
        />
        <NavButton
          collapsed={collapsed}
          icon={<BookOpen size={17} />}
          label="База знаний"
          active={workspace === "knowledge"}
          onClick={() => onNavigate("knowledge")}
        />
        <NavButton
          collapsed={collapsed}
          icon={<Clock3 size={17} />}
          label="Регулярные задания"
          active={workspace === "jobs"}
          onClick={() => onNavigate("jobs")}
        />
        <NavButton
          collapsed={collapsed}
          icon={<Tag size={17} />}
          label="Справочник тегов"
          active={workspace === "templates"}
          onClick={() => onNavigate("templates")}
        />
        <NavButton
          collapsed={collapsed}
          icon={<Settings size={17} />}
          label="Настройки"
          active={false}
          onClick={() => undefined}
        />
      </nav>
    </aside>
  );
}

function NavButton({
  collapsed,
  icon,
  label,
  active,
  onClick,
}: {
  collapsed: boolean;
  icon: ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className={active ? "active" : ""}
      onClick={onClick}
      title={collapsed ? label : undefined}
    >
      {icon}
      {!collapsed && <span>{label}</span>}
    </button>
  );
}

function ChatItem({
  chat,
  active,
  onClick,
  onTogglePin,
  onDelete,
}: {
  chat: ConversationSummary;
  active: boolean;
  onClick: () => void;
  onTogglePin: () => void;
  onDelete: () => void;
}) {
  return (
    <div className={`chat-item ${active ? "active" : ""}`}>
      <button className="chat-item-main" onClick={onClick}>
        <span className={`status-dot ${chat.status}`} />
        <span className="chat-copy">
          <strong>{chat.title}</strong>
          <small>{chat.context}</small>
        </span>
      </button>
      {!DEMO_MODE && (
        <span className="chat-item-actions">
          <button
            className={`chat-pin-button ${chat.pinned ? "active" : ""}`}
            onClick={(event) => {
              event.stopPropagation();
              onTogglePin();
            }}
            title={chat.pinned ? "Открепить диалог" : "Закрепить диалог"}
            aria-label={chat.pinned ? "Открепить диалог" : "Закрепить диалог"}
          >
            <Pin size={12} />
          </button>
          <button
            className="chat-delete-button"
            onClick={(event) => {
              event.stopPropagation();
              onDelete();
            }}
            disabled={chat.status === "processing"}
            title={
              chat.status === "processing"
                ? "Нельзя удалить во время выполнения анализа"
                : "Удалить диалог"
            }
            aria-label={`Удалить диалог ${chat.title}`}
          >
            <Trash2 size={12} />
          </button>
        </span>
      )}
      {DEMO_MODE && chat.pinned && <Pin size={12} />}
    </div>
  );
}

interface RightInspectorProps {
  collapsed: boolean;
  mobileOpen: boolean;
  activeTab: "parameters" | "sources" | "tags" | "warnings";
  context: DataContext;
  setContext: (context: DataContext) => void;
  onCloseMobile: () => void;
  onToggle: () => void;
  onTabChange: (
    tab: "parameters" | "sources" | "tags" | "warnings",
  ) => void;
  queryResult: QueryResult | null;
}

function RightInspector({
  collapsed,
  mobileOpen,
  activeTab,
  context,
  setContext,
  onCloseMobile,
  onToggle,
  onTabChange,
  queryResult,
}: RightInspectorProps) {
  if (collapsed) {
    return (
      <aside className="right-sidebar collapsed" aria-label="Контекстная панель">
        <button
          className="inspector-collapsed-toggle"
          onClick={onToggle}
          title="Развернуть панель"
        >
          <ChevronLeft size={18} />
        </button>
        <button
          className={activeTab === "parameters" ? "active" : ""}
          onClick={() => {
            onTabChange("parameters");
            onToggle();
          }}
          title="Параметры"
        >
          <Settings size={18} />
        </button>
        <button
          className={activeTab === "sources" ? "active" : ""}
          onClick={() => {
            onTabChange("sources");
            onToggle();
          }}
          title="Источники"
        >
          <Library size={18} />
        </button>
        <button
          className="warning-tab-mini"
          onClick={() => {
            onTabChange("warnings");
            onToggle();
          }}
          title="Предупреждения: 2"
        >
          <AlertCircle size={18} />
          <span>2</span>
        </button>
      </aside>
    );
  }

  return (
    <aside
      className={`right-sidebar ${mobileOpen ? "mobile-open" : ""}`}
      aria-label="Контекстная панель"
    >
      <div className="inspector-header">
        <span>Контекст результата</span>
        <button className="icon-button desktop-toggle" onClick={onToggle}>
          <PanelRightClose size={18} />
          <span className="sr-only">Свернуть панель</span>
        </button>
        <button className="icon-button mobile-only" onClick={onCloseMobile}>
          <X size={20} />
          <span className="sr-only">Закрыть панель</span>
        </button>
      </div>
      <div className="inspector-tabs" role="tablist">
        {[
          ["parameters", "Параметры"],
          ["sources", "Источники"],
          ["tags", "Теги"],
          ["warnings", "Ошибки"],
        ].map(([value, label]) => (
          <button
            key={value}
            className={activeTab === value ? "active" : ""}
            onClick={() =>
              onTabChange(value as RightInspectorProps["activeTab"])
            }
          >
            {label}
            {value === "warnings" && <span className="tab-count">2</span>}
          </button>
        ))}
      </div>

      <div className="inspector-content">
        {activeTab === "parameters" && (
          <ParametersPanel context={context} setContext={setContext} />
        )}
        {activeTab === "sources" && <SourcesPanel result={queryResult} />}
        {activeTab === "tags" && <TagsPanel />}
        {activeTab === "warnings" && <WarningsPanel />}
      </div>
    </aside>
  );
}

function ParametersPanel({
  context,
  setContext,
}: {
  context: DataContext;
  setContext: (context: DataContext) => void;
}) {
  return (
    <div className="form-stack inspector-form">
      <div className="inspector-summary">
        <span className="demo-badge">
          {DEMO_MODE ? "ДЕМО-ДАННЫЕ" : "РЕАЛЬНЫЕ ДАННЫЕ"}
        </span>
        <p>Параметры применяются к новым запросам и расчётам.</p>
      </div>
      <SelectField
        label="Территория"
        value={context.territory}
        onChange={(value) => setContext({ ...context, territory: value })}
        options={[
          "Российская Федерация",
          "Тульская область",
          "Калужская область",
        ]}
      />
      <SelectField
        label="Отчётный период"
        value={context.reportingPeriod}
        onChange={(value) =>
          setContext({ ...context, reportingPeriod: value })
        }
        options={[
          "Январь–июнь 2026",
          "I квартал 2026",
          "IV квартал 2025",
          "2025 год",
        ]}
      />
      <SelectField
        label="Период сравнения"
        value={context.comparisonPeriod}
        onChange={(value) =>
          setContext({ ...context, comparisonPeriod: value })
        }
        options={[
          "Январь–июнь 2025",
          "I квартал 2025",
          "IV квартал 2025",
          "2024 год",
        ]}
      />
      <SelectField
        label="Форма отчётности"
        value={context.form}
        onChange={(value) => setContext({ ...context, form: value })}
        options={["1-МСП", "Все формы"]}
      />
      <SelectField
        label="Бюджет"
        value="Консолидированный"
        onChange={() => undefined}
        options={["Консолидированный", "Федеральный", "Региональный"]}
      />
      <SelectField
        label="Единицы измерения"
        value="млрд ₽"
        onChange={() => undefined}
        options={["млрд ₽", "млн ₽", "тыс. ₽"]}
      />
      <div className="threshold-card">
        <div>
          <span>Порог внимания</span>
          <strong>±5%</strong>
        </div>
        <input
          type="range"
          min="1"
          max="15"
          defaultValue="5"
          aria-label="Порог внимания"
        />
      </div>
    </div>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="select-field">
      <span>{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option}>{option}</option>
        ))}
      </select>
    </label>
  );
}

function SourcesPanel({ result }: { result: QueryResult | null }) {
  if (result && !DEMO_MODE) {
    const liveSources = result.facts.flatMap((fact) =>
      fact.source.lineage?.length ? fact.source.lineage : [fact.source],
    );
    return (
      <div className="source-list">
        <div className="inspector-summary">
          <strong>{liveSources.length} подтверждённых источника</strong>
          <p>Каждое число связано с книгой, листом, ячейкой и графой формы.</p>
        </div>
        {liveSources.map((source, index) => (
          <article
            className="source-card"
            key={`${source.file}-${source.sheet}-${source.cell}-${index}`}
          >
            <div className="source-card-title">
              <span className="file-icon">
                <FileCheck2 size={16} />
              </span>
              <div>
                <strong>{source.file}</strong>
                <span className="verified">
                  <Check size={12} /> Только чтение
                </span>
              </div>
            </div>
            <dl>
              <dt>Позиция</dt>
              <dd>
                {source.sheet || "—"} · {source.cell || "—"}
              </dd>
              <dt>Строка</dt>
              <dd>{source.row_code || "—"}</dd>
              <dt>Графа</dt>
              <dd>
                {source.report_column || "—"} ·{" "}
                {source.report_column_name || "производный показатель"}
              </dd>
              <dt>Значение</dt>
              <dd>
                {source.raw_value || "—"} {source.unit || ""}
              </dd>
            </dl>
            {source.fact_id && (
              <button
                className="text-button"
                onClick={() =>
                  window.open(
                    `/api/catalog/sources/${source.fact_id}`,
                    "_blank",
                    "noopener,noreferrer",
                  )
                }
              >
                Открыть паспорт факта <ArrowRight size={13} />
              </button>
            )}
          </article>
        ))}
      </div>
    );
  }
  return (
    <div className="source-list">
      <div className="inspector-summary">
        <strong>3 источника</strong>
        <p>Все числовые результаты связаны с исходными данными.</p>
      </div>
      {sources.map((source) => (
        <article className="source-card" key={source.id}>
          <div className="source-card-title">
            <span className="file-icon">
              <FileCheck2 size={16} />
            </span>
            <div>
              <strong>{source.title}</strong>
              <span className="verified">
                <Check size={12} /> Проверен
              </span>
            </div>
          </div>
          <dl>
            <dt>Позиция</dt>
            <dd>{source.detail}</dd>
            <dt>Версия</dt>
            <dd>{source.version}</dd>
            <dt>Загружено</dt>
            <dd>{source.loadedAt}</dd>
            <dt>Правило</dt>
            <dd>{source.rule}</dd>
          </dl>
          <button className="text-button">
            Открыть первичные данные <ArrowRight size={13} />
          </button>
        </article>
      ))}
    </div>
  );
}

function TagsPanel() {
  const [query, setQuery] = useState("");
  return (
    <div className="tag-panel">
      <div className="search-field">
        <Search size={15} />
        <input
          placeholder="Поиск по тегам"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <p className="muted-copy">Доступно 18 тегов для формы 1-МСП</p>
      {tags
        .filter((item) =>
          `${item.code} ${item.name}`
            .toLowerCase()
            .includes(query.toLowerCase()),
        )
        .map((item) => (
          <button className="tag-list-item" key={item.id}>
            <span className={`tag-state ${item.status}`}>
              {item.status === "valid" ? (
                <Check size={13} />
              ) : (
                <AlertCircle size={13} />
              )}
            </span>
            <span>
              <strong>{item.name}</strong>
              <code>{`{{${item.code}}}`}</code>
            </span>
            <small>v{item.version}</small>
          </button>
        ))}
    </div>
  );
}

function WarningsPanel() {
  return (
    <div className="warning-list">
      <article className="warning-card attention">
        <div>
          <AlertCircle size={17} />
          <strong>Неполные данные по поступлениям</strong>
        </div>
        <p>
          По двум муниципальным образованиям данные загружены не полностью.
          Итоговая полнота — 97,8%.
        </p>
        <button className="text-button">Подробнее</button>
      </article>
      <article className="warning-card critical">
        <div>
          <ShieldCheck size={17} />
          <strong>Ограничение доступа</strong>
        </div>
        <p>
          Детализация до налогоплательщика недоступна для текущей роли.
        </p>
        <button className="text-button">Запросить доступ</button>
      </article>
      <article className="warning-card info">
        <div>
          <Info size={17} />
          <strong>Требуется ручная проверка</strong>
        </div>
        <p>Пояснительный текст подготовлен ИИ и не является утверждённым.</p>
      </article>
    </div>
  );
}

function HomeWorkspace({
  context,
  onNavigate,
  onShowInspector,
  onStartConversation,
}: {
  context: DataContext;
  onNavigate: (workspace: Workspace) => void;
  onShowInspector: (
    tab: "parameters" | "sources" | "tags" | "warnings",
  ) => void;
  onStartConversation: (query: string) => void;
}) {
  const [query, setQuery] = useState("");
  const submit = () => {
    if (!query.trim()) return;
    onStartConversation(query.trim());
  };

  const actions = [
    {
      icon: <Database size={20} />,
      title: "Проверить и импортировать данные",
      copy: "Просканировать 1_msp_db и посмотреть схемы",
      action: () => onNavigate("data"),
    },
    {
      icon: <FileBarChart size={20} />,
      title: "Сформировать отчёт 1-МСП",
      copy: "Подготовить проект по выбранному периоду",
      action: () => onNavigate("generator"),
    },
    {
      icon: <AlertCircle size={20} />,
      title: "Найти неблагоприятные показатели",
      copy: "Проверить отклонения и сигналы",
      action: () => onNavigate("analysis"),
    },
    {
      icon: <BarChart3 size={20} />,
      title: "Сравнить регион со средним по РФ",
      copy: "Сопоставить темпы и структуру",
      action: () => onNavigate("analysis"),
    },
    {
      icon: <WandSparkles size={20} />,
      title: "Объяснить изменение поступлений",
      copy: "Найти основные факторы изменения",
      action: () => onNavigate("analysis"),
    },
    {
      icon: <FilePlus2 size={20} />,
      title: "Создать отчёт по шаблону",
      copy: "Выбрать или загрузить Word-шаблон",
      action: () => onNavigate("templates"),
    },
  ];

  return (
    <div className="home-workspace">
      <div className="home-inner">
        <div className="eyebrow">
          <span className="demo-badge">
            {DEMO_MODE ? "ДЕМО-РЕЖИМ" : "РАБОЧИЙ MVP"}
          </span>
          {DEMO_MODE ? "Данные обезличены" : "Локальные книги 1-МСП"}
        </div>
        <h1>Добрый день, Геворг</h1>
        <p className="home-lead">
          Задайте вопрос, сформируйте отчёт или выберите готовый сценарий.
        </p>
        <button
          className="context-pill"
          onClick={() => onShowInspector("parameters")}
        >
          <span className="data-dot" />
          {context.territory} · {context.reportingPeriod}
          <Settings size={14} />
        </button>

        <div className="hero-composer">
          <textarea
            rows={3}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                submit();
              }
            }}
            placeholder="Например: сравни поступления от МСП за январь-июнь 2025 и 2026"
            aria-label="Введите вопрос или задачу"
          />
          <div className="composer-toolbar">
            <div>
              <button className="composer-tool" title="Прикрепить файл">
                <Paperclip size={17} />
              </button>
              <button className="mode-button">
                <Sparkles size={15} />
                Анализ
                <ChevronDown size={13} />
              </button>
            </div>
            <div className="composer-submit-wrap">
              <span>⌘ + Enter</span>
              <button
                className="send-button"
                disabled={!query.trim()}
                onClick={submit}
                aria-label="Отправить запрос"
              >
                <ArrowRight size={19} />
              </button>
            </div>
          </div>
        </div>

        <div className="quick-actions">
          {actions.map((action) => (
            <button className="quick-action" key={action.title} onClick={action.action}>
              <span className="quick-icon">{action.icon}</span>
              <span>
                <strong>{action.title}</strong>
                <small>{action.copy}</small>
              </span>
              <ArrowRight size={16} />
            </button>
          ))}
        </div>

        <p className="home-note">
          Суперкоманда «Собери мне отчёт по региону Якутия» откроет и заполнит
          стандартный отчёт. Официальные показатели рассчитываются проверяемыми
          правилами и запускаются после подтверждения.
        </p>
      </div>
    </div>
  );
}

export function AnalysisWorkspace({
  activeConversationId,
  onNavigate,
  onShowInspector,
  onToast,
  onResult,
  onConversationChanged,
  onOpenProject,
  onOpenStandardReport,
}: {
  activeConversationId?: string;
  onNavigate: (workspace: Workspace) => void;
  onShowInspector: (
    tab: "parameters" | "sources" | "tags" | "warnings",
  ) => void;
  onToast: (message: string) => void;
  onResult: (result: QueryResult | null) => void;
  onConversationChanged?: () => void;
  onOpenProject?: (conversationId: string) => void;
  onOpenStandardReport?: (command: string) => void;
}) {
  const storedQuery = sessionStorage.getItem("ai-vector-query");
  const defaultQuery =
    storedQuery ||
    (activeConversationId
      ? ""
      : "Сравни поступления от МСП за январь-июнь 2025 и 2026");
  const [input, setInput] = useState("");
  const [question, setQuestion] = useState(defaultQuery);
  const [mode, setMode] = useState<QueryMode>("analysis");
  const [phase, setPhase] = useState<
    "idle" | "streaming" | "done" | "stopped" | "error"
  >("idle");
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [pendingPlan, setPendingPlan] = useState<QueryPlan | null>(null);
  const [pendingResolvedMetrics, setPendingResolvedMetrics] = useState<
    ResolvedMetric[]
  >([]);
  const [conversationId, setConversationId] = useState<string | undefined>(
    activeConversationId,
  );
  const [conversationTitle, setConversationTitle] =
    useState("Анализ поступлений 1-МСП");
  const [conversationHistory, setConversationHistory] = useState<
    { question: string; answer: string; result: QueryResult }[]
  >([]);
  const [plannerMode, setPlannerMode] = useState("");
  const [plannerWarnings, setPlannerWarnings] = useState<string[]>([]);
  const [clarification, setClarification] = useState<{
    question: string;
    options: (string | ClarificationOption)[];
    turnId?: string;
  } | null>(null);
  const [errorMessage, setErrorMessage] = useState("");
  const [depth, setDepth] = useState(0);
  const [added, setAdded] = useState(false);
  const [agentStage, setAgentStage] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!activeConversationId || DEMO_MODE) return;
    let active = true;
    const controller = new AbortController();
    backendApi
      .conversation(activeConversationId, controller.signal)
      .then((conversation) => {
        if (!active) return;
        setConversationId(conversation.id);
        setConversationTitle(conversation.title);
        const readyTurns = conversation.turns
          .filter((turn) => turn.status === "ready" && turn.result)
          .map((turn) => ({
            question: turn.question,
            answer: turn.result?.answer || "",
            result: turn.result as QueryResult,
          }));
        setConversationHistory(readyTurns);
        const pending = [...conversation.turns]
          .reverse()
          .find((turn) => turn.status === "needs_clarification");
        if (pending?.clarification) {
          setQuestion(pending.question);
          setClarification({
            question: pending.clarification.question,
            options: pending.clarification.options,
            turnId: pending.id,
          });
        } else if (!storedQuery) {
          setQuestion("");
        }
      })
      .catch((error) => {
        if (!active || controller.signal.aborted) return;
        setErrorMessage(
          error instanceof Error ? error.message : "Не удалось загрузить диалог",
        );
        setPhase("error");
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [activeConversationId]);

  const runQuery = async (
    query = question,
    confirmedPlan: QueryPlan | undefined = pendingPlan || undefined,
    activeConversationId: string | undefined = conversationId,
  ) => {
    if (!query.trim()) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setQuestion(query);
    setAnswer("");
    setResult(null);
    onResult(null);
    setErrorMessage("");
    setPendingPlan(null);
    setPendingResolvedMetrics([]);
    setPhase("streaming");
    setDepth(0);
    try {
      const queryResult = await apiClient.streamQuery(
        query,
        (chunk) => setAnswer((current) => current + chunk),
        controller.signal,
        confirmedPlan,
        activeConversationId,
      );
      if (queryResult) {
        setConversationId(queryResult.conversation_id);
        setResult(queryResult);
        onResult(queryResult);
      }
      setPhase("done");
      sessionStorage.removeItem("ai-vector-query");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setPhase("stopped");
      } else {
        setErrorMessage(
          error instanceof Error ? error.message : "Неизвестная ошибка API",
        );
        setPhase("error");
      }
    }
  };

  const prepareQuery = async (query = question) => {
    if (!query.trim()) return;
    if (
      !DEMO_MODE &&
      onOpenStandardReport &&
      isStandardReportCommand(query)
    ) {
      controllerRef.current?.abort();
      onOpenStandardReport(query.trim());
      return;
    }
    if (DEMO_MODE) {
      await runQuery(query);
      return;
    }
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    if (result && answer) {
      setConversationHistory((current) =>
        current.some((turn) => turn.result.query_id === result.query_id)
          ? current
          : [...current, { question, answer, result }],
      );
    }
    setQuestion(query);
    setAnswer("");
    setResult(null);
    onResult(null);
    setPendingPlan(null);
    setPendingResolvedMetrics([]);
    setPlannerMode("");
    setPlannerWarnings([]);
    setClarification(null);
    setAgentStage("");
    setErrorMessage("");
    setPhase("streaming");
    try {
      if (activeConversationId) {
        if (ANALYTIC_AGENT_V2) {
          const initialRun = await backendApi.createAnalysisRun(
            activeConversationId,
            query,
            controller.signal,
          );
          setActiveRunId(initialRun.run_id);
          const completedRun = await backendApi.waitForAnalysisRun(
            initialRun,
            (stage) => setAgentStage(stage),
            controller.signal,
          );
          setActiveRunId(null);
          setConversationId(activeConversationId);
          if (
            completedRun.status === "needs_clarification" &&
            completedRun.clarification
          ) {
            setClarification({
              question: completedRun.clarification.question,
              options: completedRun.clarification.options || [],
              turnId: completedRun.run_id,
            });
            setPhase("idle");
            onConversationChanged?.();
            return;
          }
          if (completedRun.status === "no_data") {
            const refusal =
              completedRun.result?.answer ||
              completedRun.error?.message ||
              "Для запроса нет проверенных данных.";
            setAnswer(refusal);
            setPlannerWarnings(completedRun.result?.warnings || [refusal]);
            setPhase("done");
            onConversationChanged?.();
            return;
          }
          if (completedRun.status !== "ready" || !completedRun.result) {
            throw new Error(
              completedRun.error?.message ||
                completedRun.result?.answer ||
                "Аналитический запуск не завершён",
            );
          }
          const turnResult = completedRun.result;
          setPlannerMode(turnResult.planner_mode || "analytic_agent_v2");
          setPlannerWarnings(turnResult.warnings || []);
          setAnswer(turnResult.answer);
          setResult(turnResult);
          onResult(turnResult);
          setPhase("done");
          setAdded(false);
          if (conversationTitle === "Новый диалог") {
            setConversationTitle(
              query.length > 64 ? `${query.slice(0, 61)}…` : query,
            );
          }
          sessionStorage.removeItem("ai-vector-query");
          onConversationChanged?.();
          return;
        }
        const turnResult = await backendApi.createConversationTurn(
          activeConversationId,
          query,
          controller.signal,
        );
        setConversationId(activeConversationId);
        setPlannerMode(turnResult.planner_mode || "");
        setPlannerWarnings(turnResult.warnings || []);
        if (turnResult.status === "needs_clarification") {
          setClarification({
            question:
              turnResult.clarification_question ||
              "Уточните параметры аналитического запроса.",
            options: turnResult.clarification_options || [],
            turnId: turnResult.turn_id,
          });
          setPhase("idle");
          onConversationChanged?.();
          return;
        }
        setAnswer(turnResult.answer);
        setResult(turnResult);
        onResult(turnResult);
        setPhase("done");
        setAdded(false);
        if (conversationTitle === "Новый диалог") {
          setConversationTitle(
            query.length > 64 ? `${query.slice(0, 61)}…` : query,
          );
        }
        sessionStorage.removeItem("ai-vector-query");
        onConversationChanged?.();
        return;
      }
      const planned = await backendApi.planQuery(
        query,
        conversationId,
        controller.signal,
      );
      setConversationId(planned.conversation_id);
      setPlannerMode(planned.planner_mode);
      setPlannerWarnings(planned.warnings);
      setPendingResolvedMetrics(planned.resolved_metrics);
      if (planned.status === "needs_clarification") {
        setClarification({
          question:
            planned.clarification_question ||
            "Уточните параметры аналитического запроса.",
          options: planned.clarification_options || [],
        });
        setPhase("idle");
        return;
      }
      if (!planned.plan) {
        throw new Error("Планировщик не вернул проверенный план");
      }
      if (planned.requires_confirmation) {
        setPendingPlan(planned.plan);
        setPhase("idle");
      } else {
        await runQuery(
          query,
          planned.plan,
          planned.conversation_id,
        );
      }
    } catch (planError) {
      if (planError instanceof DOMException && planError.name === "AbortError") {
        setPhase("stopped");
      } else {
        setErrorMessage(
          planError instanceof Error
            ? planError.message
            : "Не удалось построить план",
        );
        setPhase("error");
      }
    }
  };

  const resolveClarification = async (
    option: string | ClarificationOption,
  ) => {
    if (!activeConversationId || !clarification?.turnId) {
      const label = typeof option === "string" ? option : option.label;
      setInput(label);
      await prepareQuery(label);
      return;
    }
    const clarificationAnswer =
      typeof option === "string"
        ? { text: option }
        : { option_id: option.id };
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setPhase("streaming");
    setErrorMessage("");
    try {
      if (ANALYTIC_AGENT_V2) {
        const resumed = await backendApi.clarifyAnalysisRun(
          clarification.turnId,
          clarificationAnswer,
          controller.signal,
        );
        setActiveRunId(resumed.run_id);
        const completed = await backendApi.waitForAnalysisRun(
          resumed,
          (stage) => setAgentStage(stage),
          controller.signal,
        );
        setActiveRunId(null);
        if (completed.status === "needs_clarification" && completed.clarification) {
          setClarification({
            question: completed.clarification.question,
            options: completed.clarification.options || [],
            turnId: completed.run_id,
          });
          setPhase("idle");
          return;
        }
        if (completed.status === "no_data") {
          const refusal =
            completed.result?.answer ||
            completed.error?.message ||
            "Для запроса нет проверенных данных.";
          setClarification(null);
          setAnswer(refusal);
          setPlannerWarnings(completed.result?.warnings || [refusal]);
          setPhase("done");
          onConversationChanged?.();
          return;
        }
        if (completed.status !== "ready" || !completed.result) {
          throw new Error(
            completed.error?.message ||
              completed.result?.answer ||
              "Не удалось продолжить исследование",
          );
        }
        const turnResult = completed.result;
        setClarification(null);
        setAnswer(turnResult.answer);
        setResult(turnResult);
        onResult(turnResult);
        setPlannerMode(turnResult.planner_mode || "analytic_agent_v2");
        setPlannerWarnings(turnResult.warnings || []);
        setPhase("done");
        setAdded(false);
        onConversationChanged?.();
        return;
      }
      const turnResult = await backendApi.answerClarification(
        activeConversationId,
        clarification.turnId,
        clarificationAnswer,
        controller.signal,
      );
      setClarification(null);
      setAnswer(turnResult.answer);
      setResult(turnResult);
      onResult(turnResult);
      setPlannerMode(turnResult.planner_mode || "clarification_patch");
      setPlannerWarnings(turnResult.warnings || []);
      setPhase("done");
      setAdded(false);
      if (conversationTitle === "Новый диалог") {
        setConversationTitle(
          question.length > 64 ? `${question.slice(0, 61)}…` : question,
        );
      }
      onConversationChanged?.();
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setPhase("stopped");
        return;
      }
      setErrorMessage(
        error instanceof Error ? error.message : "Не удалось применить уточнение",
      );
      setPhase("error");
    }
  };

  useEffect(() => {
    const timer = window.setTimeout(() => void prepareQuery(defaultQuery), 0);
    return () => {
      window.clearTimeout(timer);
      controllerRef.current?.abort();
    };
    // This deliberately runs once for the selected demo conversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rows =
    depth === 0
      ? revenueRows
      : depth === 1
        ? okvedRows
        : [
            {
              name: "Группа плательщиков А-014",
              current: 2.7,
              previous: 4.4,
              change: -38.6,
              contribution: -1.7,
              signal: "adverse" as const,
            },
            {
              name: "Группа плательщиков М-208",
              current: 1.8,
              previous: 2.5,
              change: -28,
              contribution: -0.7,
              signal: "adverse" as const,
            },
            {
              name: "Прочие обезличенные группы",
              current: 2.3,
              previous: 2.5,
              change: -8,
              contribution: -0.2,
              signal: "attention" as const,
            },
          ];

  const addCurrentResultToProject = async () => {
    if (!activeConversationId || !result?.turn_id) {
      onToast("Сначала дождитесь готового результата");
      return;
    }
    try {
      await backendApi.addProjectBlock(
        activeConversationId,
        result.turn_id,
      );
      setAdded(true);
      onToast("Результат добавлен в аналитический проект");
      onConversationChanged?.();
    } catch (error) {
      onToast(
        error instanceof Error ? error.message : "Не удалось добавить результат",
      );
    }
  };

  const saveCurrentScenario = async () => {
    if (!activeConversationId) {
      onToast("Сценарий доступен для сохранённого диалога");
      return;
    }
    try {
      const scenario = await backendApi.saveScenario(activeConversationId);
      onToast(`Сценарий «${scenario.title}» сохранён`);
      onConversationChanged?.();
    } catch (error) {
      onToast(
        error instanceof Error ? error.message : "Не удалось сохранить сценарий",
      );
    }
  };

  return (
    <div className="conversation-workspace">
      <div className="conversation-header">
        <div>
          <div className="conversation-title-row">
            <h1>{conversationTitle}</h1>
            <span className="status-chip draft">Черновик</span>
          </div>
          <p>Многоходовый анализ по данным и источникам 1-МСП</p>
        </div>
        <div className="conversation-actions">
          {!DEMO_MODE &&
            activeConversationId &&
            conversationHistory.length > 0 &&
            onOpenProject && (
              <button
                className="primary-button"
                onClick={() => onOpenProject(activeConversationId)}
              >
                <FileText size={16} /> Сформировать отчёт
              </button>
            )}
          <button className="outline-button" onClick={() => onNavigate("home")}>
            <Plus size={16} /> Новый запрос
          </button>
          <button className="icon-button">
            <MoreHorizontal size={19} />
            <span className="sr-only">Другие действия</span>
          </button>
        </div>
      </div>

      <div className="conversation-scroll">
        {conversationHistory.length === 0 &&
          !question &&
          phase === "idle" && (
            <div className="conversation-empty-state">
              <Bot size={22} />
              <h2>Новый аналитический диалог</h2>
              <p>
                Задайте первый вопрос. Следующие вопросы будут наследовать
                показатель, период, территорию и аналитический срез.
              </p>
            </div>
          )}
        {conversationHistory.map((turn) => (
          <div
            className="conversation-history-turn"
            key={turn.result.query_id}
          >
            <div className="user-message">
              <div className="avatar small">ГМ</div>
              <div>
                <span>Вы</span>
                <p>{turn.question}</p>
              </div>
            </div>
            <div className="assistant-message">
              <div className="assistant-avatar">
                <Bot size={18} />
              </div>
              <div className="assistant-content">
                <div className="assistant-meta">
                  <strong>ИИ-ВЕКТОР</strong>
                  <span>Предыдущий ход этой беседы</span>
                </div>
                {turn.result.query_plan && (
                  <QueryPlanSummary
                    plan={turn.result.query_plan}
                    resolvedMetrics={turn.result.resolved_metrics}
                    plannerMode={turn.result.planner_mode}
                    confirmed
                  />
                )}
                <p className="summary-text">{turn.answer}</p>
                {turn.result.analysis_sections &&
                  turn.result.analysis_sections.length > 0 && (
                    <AnalysisSections
                      sections={turn.result.analysis_sections}
                      showResolvedEntities={
                        turn.result.query_plan?.intent === "catalog"
                      }
                    />
                  )}
                {turn.result.series.length > 0 && (
                    <TrendSeriesChart series={turn.result.series} />
                  )}
                {turn.result.facts.length > 0 && (
                  <LiveFactResults
                    result={turn.result}
                    onShowSources={() => {
                      onResult(turn.result);
                      onShowInspector("sources");
                    }}
                  />
                )}
                {!DEMO_MODE && activeConversationId && turn.result.turn_id && (
                  <div className="result-actions compact-history-actions">
                    <button
                      className="outline-button"
                      onClick={async () => {
                        try {
                          await backendApi.addProjectBlock(
                            activeConversationId,
                            turn.result.turn_id as string,
                          );
                          onToast("Предыдущий результат добавлен в проект");
                          onConversationChanged?.();
                        } catch (error) {
                          onToast(
                            error instanceof Error
                              ? error.message
                              : "Не удалось добавить результат",
                          );
                        }
                      }}
                    >
                      <FilePlus2 size={15} /> Добавить в отчёт
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}
        {question && (
          <div className="user-message">
            <div className="avatar small">ГМ</div>
            <div>
              <span>Вы</span>
              <p>{question}</p>
            </div>
          </div>
        )}

        {(question || phase !== "idle" || clarification) && (
        <div className="assistant-message">
          <div className="assistant-avatar">
            <Bot size={18} />
          </div>
          <div className="assistant-content">
            <div className="assistant-meta">
              <strong>ИИ-ВЕКТОР</strong>
              <span>
                {DEMO_MODE
                  ? "Анализ на демонстрационных данных"
                  : "Проверяемый анализ реальных книг 1-МСП"}
              </span>
            </div>

            {pendingPlan && (
              <div className="plan-confirmation">
                <QueryPlanSummary
                  plan={pendingPlan}
                  resolvedMetrics={pendingResolvedMetrics}
                  plannerMode={plannerMode}
                />
                {plannerWarnings.map((warning) => (
                  <p className="muted-copy" key={warning}>
                    {warning}
                  </p>
                ))}
                <div className="plan-confirmation-actions">
                  <span>
                    Проверьте интерпретацию перед выполнением расчёта.
                  </span>
                  <button
                    className="primary-button compact"
                    onClick={() => void runQuery(question, pendingPlan)}
                  >
                    <Play size={14} /> Выполнить анализ
                  </button>
                </div>
              </div>
            )}

            {clarification && (
              <div className="clarification-card" role="status">
                <div>
                  <MessageSquarePlus size={17} />
                  <strong>Нужно одно уточнение</strong>
                </div>
                <p>{clarification.question}</p>
                {clarification.options.length > 0 && (
                  <div className="clarification-options">
                    {clarification.options.map((option) => (
                      <button
                        className="outline-button compact"
                        key={
                          typeof option === "string" ? option : option.id
                        }
                        onClick={() => void resolveClarification(option)}
                      >
                        {typeof option === "string" ? option : option.label}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {phase === "streaming" && !answer && (
              <div className="thinking-state">
                <span className="thinking-spinner" />
                <span>
                  <strong>
                    {ANALYTIC_AGENT_V2 && agentStage
                      ? agentStage
                      : "Анализирую показатели"}
                  </strong>
                  <small>Проверяю источники и рассчитываю вклад факторов…</small>
                </span>
              </div>
            )}

            {answer && (
              <>
                {result?.query_plan && (
                  <QueryPlanSummary
                    plan={result.query_plan}
                    resolvedMetrics={result.resolved_metrics}
                    plannerMode={result.planner_mode || plannerMode}
                    confirmed
                  />
                )}
                <ResultSection
                  icon={<Sparkles size={17} />}
                  title="Краткий вывод"
                  action={
                    <button className="text-button">
                      <RefreshCw size={13} /> Переформулировать
                    </button>
                  }
                >
                  <p className="summary-text">
                    {answer}
                    {phase === "streaming" && (
                      <span className="streaming-caret" />
                    )}
                  </p>
                  <div className="verification-note">
                    <Info size={15} />
                    Пояснение подготовлено ИИ. Числовые значения получены
                    детерминированным расчётом.
                  </div>
                </ResultSection>

                {!DEMO_MODE && result?.research_plan && (
                  <ResultSection
                    icon={<ListFilter size={17} />}
                    title="План исследования"
                  >
                    <p className="muted-copy">
                      {result.research_plan.objective}
                    </p>
                    <ul>
                      {result.research_plan.operations.map((operation) => (
                        <li key={operation.id}>
                          {operation.goal}
                        </li>
                      ))}
                    </ul>
                  </ResultSection>
                )}

                {!DEMO_MODE &&
                  result?.hypotheses &&
                  result.hypotheses.length > 0 && (
                    <ResultSection
                      icon={<Info size={17} />}
                      title="Гипотезы"
                    >
                      {result.hypotheses.map((hypothesis) => (
                        <p className="muted-copy" key={hypothesis.hypothesis_id}>
                          <strong>Гипотеза.</strong> {hypothesis.text}
                        </p>
                      ))}
                    </ResultSection>
                  )}

                {!DEMO_MODE &&
                  result?.claims &&
                  result.claims.length > 0 && (
                    <div className="verification-note">
                      <ShieldCheck size={15} />
                      {result.claims.length} выводов проверены по evidence:{" "}
                      {result.claims
                        .flatMap((claim) => claim.evidence_ids)
                        .filter((item, index, all) => all.indexOf(item) === index)
                        .join(", ")}
                    </div>
                  )}

                {!DEMO_MODE &&
                  result?.catalog_items &&
                  result.catalog_items.length > 0 && (
                    <CatalogResultList result={result} />
                  )}

                {!DEMO_MODE &&
                  result?.analysis_sections &&
                  result.analysis_sections.length > 0 && (
                    <AnalysisSections
                      sections={result.analysis_sections}
                      showResolvedEntities={
                        result.query_plan?.intent === "catalog"
                      }
                    />
                  )}

                {!DEMO_MODE && result && result.facts.length > 0 && (
                  <>
                    {result.series.length > 0 && (
                        <TrendSeriesChart series={result.series} />
                      )}
                    <LiveFactResults
                      result={result}
                      onShowSources={() => onShowInspector("sources")}
                    />
                  </>
                )}

                {DEMO_MODE && <ResultSection
                  icon={<BarChart3 size={17} />}
                  title="Ключевые показатели"
                >
                  <div className="metric-grid">
                    {metrics.map((metric) => (
                      <article className="metric-card" key={metric.id}>
                        <div className="metric-top">
                          <span>{metric.label}</span>
                          <SignalBadge signal={metric.signal} iconOnly />
                        </div>
                        <strong>{metric.value}</strong>
                        <span className={`metric-change ${metric.signal}`}>
                          {metric.signal === "adverse" && (
                            <ArrowDown size={13} />
                          )}
                          {metric.change}
                        </span>
                        <button
                          className="calculation-link"
                          onClick={() => onShowInspector("sources")}
                        >
                          Показать расчёт <ChevronRight size={13} />
                        </button>
                      </article>
                    ))}
                  </div>
                </ResultSection>}

                {DEMO_MODE && <ResultSection
                  icon={<BarChart3 size={17} />}
                  title="Динамика поступлений"
                  action={<span className="section-unit">млрд ₽</span>}
                >
                  <RevenueChart />
                </ResultSection>}

                {DEMO_MODE && <ResultSection
                  icon={<Table2 size={17} />}
                  title={
                    depth === 0
                      ? "Вклад налогов в изменение"
                      : depth === 1
                        ? "Детализация по видам деятельности"
                        : "Обезличенные группы налогоплательщиков"
                  }
                  action={
                    <button className="outline-button compact">
                      <Download size={14} /> Excel
                    </button>
                  }
                >
                  {depth > 0 && (
                    <div className="drill-breadcrumb">
                      <button onClick={() => setDepth(0)}>
                        Поступления региона
                      </button>
                      <ChevronRight size={13} />
                      <button onClick={() => setDepth(1)}>
                        Налог на прибыль
                      </button>
                      {depth === 2 && (
                        <>
                          <ChevronRight size={13} />
                          <span>Обрабатывающие производства</span>
                        </>
                      )}
                    </div>
                  )}
                  <RevenueTable rows={rows} />
                  {depth < 2 ? (
                    <button
                      className="drill-button"
                      onClick={() => setDepth((value) => value + 1)}
                    >
                      <span className="drill-icon">
                        <ArrowDown size={16} />
                      </span>
                      <span>
                        <strong>Углубить анализ</strong>
                        <small>
                          {depth === 0
                            ? "Перейти к видам деятельности по ОКВЭД"
                            : "Перейти к обезличенным группам плательщиков"}
                        </small>
                      </span>
                      <ChevronRight size={17} />
                    </button>
                  ) : (
                    <div className="access-limit">
                      <ShieldCheck size={17} />
                      <span>
                        <strong>Следующий уровень недоступен</strong>
                        <small>
                          Для просмотра конкретных плательщиков требуется роль с
                          расширенным доступом.
                        </small>
                      </span>
                      <button className="text-button">Запросить доступ</button>
                    </div>
                  )}
                </ResultSection>}

                {DEMO_MODE && <div className="warning-inline">
                  <AlertCircle size={18} />
                  <div>
                    <strong>Есть ограничения результата</strong>
                    <p>
                      Полнота данных составляет 97,8%. Проверьте два источника
                      перед использованием в официальном отчёте.
                    </p>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => onShowInspector("warnings")}
                  >
                    Открыть
                  </button>
                </div>}

                <div className="result-actions">
                  <button
                    className={`primary-button ${added ? "success-button" : ""}`}
                    onClick={() => void addCurrentResultToProject()}
                  >
                    {added ? <Check size={16} /> : <FilePlus2 size={16} />}
                    {added ? "Добавлено в отчёт" : "Добавить в отчёт"}
                  </button>
                  <button
                    className="outline-button"
                    onClick={() =>
                      activeConversationId && onOpenProject
                        ? onOpenProject(activeConversationId)
                        : onNavigate("report")
                    }
                  >
                    <FileText size={16} /> Открыть проект
                  </button>
                  <button
                    className="outline-button"
                    onClick={() => void saveCurrentScenario()}
                  >
                    <Pin size={16} /> Сохранить сценарий
                  </button>
                </div>
              </>
            )}

            {phase === "stopped" && (
              <div className="stopped-message">
                <Square size={15} />
                Формирование ответа остановлено. Доступен частичный результат.
                <button
                  className="text-button"
                  onClick={() => void prepareQuery()}
                >
                  Повторить
                </button>
              </div>
            )}

            {phase === "error" && (
              <div className="error-message">
                <AlertCircle size={17} />
                <span>
                  Не удалось выполнить запрос: {errorMessage || "backend недоступен"}.
                  {!DEMO_MODE && " Проверьте импорт на экране «Данные»."}
                </span>
                <button
                  className="text-button"
                  onClick={() => void prepareQuery()}
                >
                  Повторить
                </button>
              </div>
            )}
          </div>
        </div>
        )}
      </div>

      <PromptComposer
        value={input}
        setValue={setInput}
        mode={mode}
        setMode={setMode}
        streaming={phase === "streaming"}
        onStop={() => {
          if (ANALYTIC_AGENT_V2 && activeRunId) {
            void backendApi.cancelAnalysisRun(activeRunId);
          }
          controllerRef.current?.abort();
        }}
        onSubmit={() => {
          if (!input.trim()) return;
          if (activeConversationId && clarification?.turnId) {
            void resolveClarification(input.trim());
          } else {
            void prepareQuery(input);
          }
          setInput("");
        }}
      />
    </div>
  );
}

function QueryPlanSummary({
  plan,
  resolvedMetrics = [],
  plannerMode,
  confirmed = false,
}: {
  plan: QueryPlan;
  resolvedMetrics?: ResolvedMetric[];
  plannerMode?: string;
  confirmed?: boolean;
}) {
  const intentLabels: Record<string, string> = {
    value: "значение",
    compare: "сравнение периодов",
    trend: "многолетняя динамика",
    overview: "общая ситуация",
    rank: "рейтинг по уровню и росту",
    outliers: "диагностика выбросов",
    share: "расчёт доли",
    source: "поиск источника",
    rank_growth: "лидеры по темпу",
    rank_difference: "лидеры по отклонению",
    catalog: "проверка доступности",
  };
  const catalogTargetLabels: Record<string, string> = {
    overview: "состав данных",
    periods: "периоды",
    territories: "регионы",
    metrics: "показатели и налоги",
    okved: "строки и ОКВЭД",
  };
  const groupLabels: Record<string, string> = {
    all_msp: "все МСП",
    micro: "микропредприятия",
    small_and_medium_without_micro: "малые и средние без микро",
  };
  const detailsByCode = new Map(
    resolvedMetrics.map((metric) => [metric.semantic_code, metric]),
  );
  return (
    <div className="query-plan-card">
      <div>
        <span className="section-icon">
          <ClipboardCheck size={16} />
        </span>
        <strong>Понято как</strong>
        {plannerMode && (
          <span className="verified">
            <Sparkles size={12} />{" "}
            {plannerMode.startsWith("llm:") ? "GLM" : "Правила"}
          </span>
        )}
        {confirmed && (
          <span className="verified">
            <Check size={12} /> План подтверждён
          </span>
        )}
      </div>
      <dl>
        <dt>Задача</dt>
        <dd>
          {intentLabels[plan.intent] || plan.intent}
          {plan.catalog_target
            ? ` · ${catalogTargetLabels[plan.catalog_target]}`
            : ""}
        </dd>
        <dt>Периоды</dt>
        <dd>
          {plan.period_labels?.length
            ? plan.period_labels.join(" · ")
            : plan.periods.length
              ? plan.periods.join(" · ")
              : "определяются по источникам"}
        </dd>
        <dt>Показатели</dt>
        <dd>
          {plan.metric_codes.length
            ? plan.metric_codes
                .map((code) => {
                  const metric = detailsByCode.get(code);
                  if (!metric) return code;
                  const column = metric.report_column_number
                    ? ` · графа ${metric.report_column_number}`
                    : "";
                  return `${metric.display_name}${column}`;
                })
                .join(", ")
            : "все доступные"}
        </dd>
        <dt>Строки / ОКВЭД</dt>
        <dd>
          {plan.intent === "catalog" && plan.catalog_target === "okved"
            ? "весь доступный справочник"
            : plan.okved_codes?.length
            ? plan.okved_codes.join(", ")
            : plan.row_codes.join(", ")}
        </dd>
        <dt>Группа</dt>
        <dd>
          {plan.enterprise_groups
            .map((item) => groupLabels[item] || item)
            .join(", ")}
        </dd>
        <dt>Территория</dt>
        <dd>
          {plan.intent === "catalog" && plan.catalog_target === "territories"
            ? "все доступные субъекты"
            : plan.territories.join(", ")}
          {plan.include_top_territory ? " + регион с максимумом" : ""}
          {plan.include_bottom_territory ? " + регион с минимумом" : ""}
        </dd>
      </dl>
    </div>
  );
}

function factDisplayValue(fact: QueryFact) {
  const raw = Number(fact.value);
  const inBillions = fact.unit === "тыс. руб.";
  return {
    value: new Intl.NumberFormat("ru-RU", {
      maximumFractionDigits: 2,
      minimumFractionDigits: 2,
    }).format(inBillions ? raw / 1_000_000 : raw),
    unit: inBillions ? "млрд ₽" : fact.unit,
  };
}

function CatalogResultList({ result }: { result: QueryResult }) {
  const items = result.catalog_items || [];
  return (
    <ResultSection
      icon={<ListFilter size={17} />}
      title="Доступный каталог"
      action={<span className="verified">{items.length} позиций показано</span>}
    >
      <div className="catalog-result-list">
        {items.map((item) => (
          <article key={`${item.kind}-${item.code}`}>
            <code>{item.code}</code>
            <div>
              <strong>{item.label}</strong>
              <span>{item.detail}</span>
            </div>
          </article>
        ))}
      </div>
    </ResultSection>
  );
}

function analysisNumber(value?: string | null, percent = false) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return value;
  if (percent) {
    return `${number.toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      signDisplay: "exceptZero",
    })}%`;
  }
  const divisor = Math.abs(number) >= 1_000_000 ? 1_000_000 : 1;
  const suffix = divisor === 1_000_000 ? " млрд ₽" : "";
  return (
    (number / divisor).toLocaleString("ru-RU", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
      signDisplay: "exceptZero",
    }) + suffix
  );
}

export function AnalysisSections({
  sections,
  showResolvedEntities = false,
}: {
  sections: AnalysisSection[];
  showResolvedEntities?: boolean;
}) {
  const visibleSections = sections.filter(
    (section) =>
      section.kind !== "resolved_entities" || showResolvedEntities,
  );
  const resolvedKindLabels: Record<string, string> = {
    metric: "Показатель",
    industry: "Строка / ОКВЭД",
    territory: "Территория",
    period: "Период",
  };
  return (
    <>
      {visibleSections.map((section) => {
        const isRegionalAverage =
          section.kind === "regional_average_growth";
        const isRegionalIndustry =
          section.kind === "regional_industry_growth" ||
          section.kind === "regional_industry_growth_worst";
        const isResolvedEntities = section.kind === "resolved_entities";
        return (
          <ResultSection
            key={`${section.kind}-${section.title}`}
            icon={<Table2 size={17} />}
            title={section.title}
          >
            <div className="analysis-table-scroll">
              <table className="analysis-result-table">
                <thead>
                  {isResolvedEntities ? (
                    <tr>
                      <th>Тип</th>
                      <th>Код</th>
                      <th>Наименование</th>
                      <th>Единица / детализация</th>
                    </tr>
                  ) : isRegionalAverage ? (
                    <tr>
                      <th>Отрасль</th>
                      <th>Регионов</th>
                      <th>Средний прирост</th>
                      <th>Медиана</th>
                      <th>Взвешенный прирост</th>
                      <th>Минимум</th>
                      <th>Максимум</th>
                    </tr>
                  ) : (
                    <tr>
                      <th>{isRegionalIndustry ? "Регион" : "Объект"}</th>
                      <th>Текущее значение</th>
                      <th>Предыдущее</th>
                      <th>Изменение</th>
                      <th>Рост</th>
                      {section.rows.some((row) => row.score != null) && (
                        <th>Индекс</th>
                      )}
                    </tr>
                  )}
                </thead>
                <tbody>
                  {section.rows.map((row, index) =>
                    isResolvedEntities ? (
                      <tr key={`${row.kind}-${row.code}-${index}`}>
                        <td>
                          {resolvedKindLabels[row.kind || ""] ||
                            row.kind ||
                            "—"}
                        </td>
                        <td><code>{row.code ?? "—"}</code></td>
                        <td><strong>{row.name || "—"}</strong></td>
                        <td>
                          {row.unit ||
                            row.okved ||
                            row.territory_type ||
                            "—"}
                        </td>
                      </tr>
                    ) : isRegionalAverage ? (
                      <tr key={`${row.entity || "row"}-${index}`}>
                        <td>
                          <strong>{row.entity}</strong>
                        </td>
                        <td>{row.regions_count ?? "—"}</td>
                        <td>{analysisNumber(row.growth_rate, true)}</td>
                        <td>
                          {analysisNumber(row.median_growth_rate, true)}
                        </td>
                        <td>
                          {analysisNumber(row.weighted_growth_rate, true)}
                        </td>
                        <td>
                          {row.minimum_region
                            ? `${row.minimum_region.entity}: ${analysisNumber(
                                row.minimum_region.growth_rate,
                                true,
                              )}`
                            : "—"}
                        </td>
                        <td>
                          {row.maximum_region
                            ? `${row.maximum_region.entity}: ${analysisNumber(
                                row.maximum_region.growth_rate,
                                true,
                              )}`
                            : "—"}
                        </td>
                      </tr>
                    ) : (
                      <tr key={`${row.entity || "row"}-${index}`}>
                        <td>
                          <strong>
                            {isRegionalIndustry
                              ? row.region ||
                                (row.industry &&
                                (row.entity || "").startsWith(
                                  `${row.industry} — `,
                                )
                                  ? (row.entity || "").slice(
                                      `${row.industry} — `.length,
                                    )
                                  : row.entity)
                              : row.entity || "—"}
                          </strong>
                          {row.low_base && <small>малая база</small>}
                          {row.flagged && <small>выброс ≥ 2,5</small>}
                        </td>
                        <td>{analysisNumber(row.current)}</td>
                        <td>{analysisNumber(row.previous)}</td>
                        <td>{analysisNumber(row.difference)}</td>
                        <td>{analysisNumber(row.growth_rate, true)}</td>
                        {section.rows.some(
                          (item) => item.score != null,
                        ) && <td>{analysisNumber(row.score)}</td>}
                      </tr>
                    ),
                  )}
                </tbody>
              </table>
            </div>
            <p className="methodology-note">{section.methodology}</p>
            {section.threshold_exceeded === false && (
              <p className="muted-copy">
                Диагностический порог не превышен; показаны максимальные
                наблюдаемые значения индекса.
              </p>
            )}
          </ResultSection>
        );
      })}
    </>
  );
}

type EdgeSafeAxisTickProps = {
  x?: number;
  y?: number;
  index?: number;
  visibleTicksCount?: number;
  payload?: { value?: string | number };
  fontSize?: number;
};

export function EdgeSafeAxisTick({
  x = 0,
  y = 0,
  index = 0,
  visibleTicksCount = 1,
  payload,
  fontSize = 11,
}: EdgeSafeAxisTickProps) {
  const anchor =
    visibleTicksCount <= 1
      ? "middle"
      : index === 0
        ? "start"
        : index === visibleTicksCount - 1
          ? "end"
          : "middle";
  return (
    <text
      x={x}
      y={y}
      dy="0.9em"
      textAnchor={anchor}
      fill="var(--text-tertiary)"
      fontSize={fontSize}
    >
      {String(payload?.value ?? "")}
    </text>
  );
}

export function TrendSeriesChart({ series }: { series: QuerySeries[] }) {
  const palette = [
    "#2563eb",
    "#dc2626",
    "#059669",
    "#7c3aed",
    "#d97706",
    "#0891b2",
    "#db2777",
    "#4f46e5",
  ];
  const [chartMode, setChartMode] = useState<"change" | "absolute">(
    "change",
  );
  const [activePeriod, setActivePeriod] = useState<string | null>(null);
  const relativeMode = series.length > 1 && chartMode === "change";
  const sharedUnit = series.every(
    (item) => item.unit === series[0]?.unit,
  );
  const unit = !sharedUnit
    ? "разные единицы"
    : series[0]?.unit === "тыс. руб."
      ? "млрд ₽"
      : series[0]?.unit;
  const chartTitle = useMemo(() => {
    if (series.length > 1 && series.every((item) => item.row_code !== 1010)) {
      return "Динамика по выбранным отраслям";
    }
    if (new Set(series.map((item) => item.territory)).size > 1) {
      return "Динамика по территориям";
    }
    if (new Set(series.map((item) => item.metric_code)).size > 1) {
      return "Динамика показателей";
    }
    return "Многолетняя динамика";
  }, [series]);
  const formatNumber = (
    value: number,
    options: Intl.NumberFormatOptions = {},
  ) =>
    new Intl.NumberFormat("ru-RU", {
      maximumFractionDigits: 2,
      ...options,
    }).format(value);
  const presentation = useMemo(() => {
    const parts = series.map((item) =>
      item.label.split(" · ").map((part) => part.trim()),
    );
    let commonLength = 0;
    if (series.length > 1 && parts.length > 0) {
      const shortest = Math.min(...parts.map((item) => item.length));
      while (
        commonLength < shortest - 1 &&
        parts.every(
          (item) => item[commonLength] === parts[0][commonLength],
        )
      ) {
        commonLength += 1;
      }
    }
    const cleanName = (value: string) =>
      value
        .replace(/\s*[-–—]\s*всего\s*$/iu, "")
        .replace(/\s{2,}/g, " ")
        .trim();
    return {
      context:
        commonLength > 0
          ? parts[0].slice(0, commonLength).join(" · ")
          : "",
      names: new Map(
        series.map((item, index) => [
          item.key,
          cleanName(
            commonLength > 0
              ? parts[index].slice(commonLength).join(" · ")
              : item.label,
          ),
        ]),
      ),
    };
  }, [series]);
  const rows = useMemo(() => {
    const byPeriod = new Map<string, Record<string, string | number>>();
    series.forEach((item) => {
      item.points.forEach((point) => {
        const row = byPeriod.get(point.period_end) || {
          period_end: point.period_end,
          period: point.label,
        };
        row[item.key] =
          item.unit === "тыс. руб."
            ? Number(point.value) / 1_000_000
            : Number(point.value);
        byPeriod.set(point.period_end, row);
      });
    });
    return [...byPeriod.values()].sort((a, b) =>
      String(a.period_end).localeCompare(String(b.period_end)),
    );
  }, [series]);
  const chartRows = useMemo(() => {
    if (!relativeMode) return rows;
    const firstValues = new Map(
      series.map((item) => {
        const first = item.points[0];
        return [
          item.key,
          first
            ? item.unit === "тыс. руб."
              ? Number(first.value) / 1_000_000
              : Number(first.value)
            : 0,
        ];
      }),
    );
    return rows.map((row) => {
      const normalized = { ...row };
      series.forEach((item) => {
        const first = firstValues.get(item.key) || 0;
        const current = Number(row[item.key]);
        normalized[item.key] =
          Number.isFinite(current) && first !== 0
            ? ((current / first) - 1) * 100
            : 0;
      });
      return normalized;
    });
  }, [relativeMode, rows, series]);
  const selectedChartRow =
    chartRows.find((row) => row.period_end === activePeriod) ||
    chartRows[chartRows.length - 1];
  const selectedAbsoluteRow =
    rows.find((row) => row.period_end === selectedChartRow?.period_end) ||
    rows[rows.length - 1];

  return (
    <ResultSection
      icon={<BarChart3 size={17} />}
      title={chartTitle}
      action={
        <div className="trend-chart-actions">
          {series.length > 1 && (
            <div
              className="trend-mode-switch"
              role="group"
              aria-label="Режим отображения графика"
            >
              <button
                type="button"
                className={relativeMode ? "active" : ""}
                aria-pressed={relativeMode}
                onClick={() => setChartMode("change")}
              >
                Изменение, %
              </button>
              <button
                type="button"
                className={!relativeMode ? "active" : ""}
                aria-pressed={!relativeMode}
                onClick={() => setChartMode("absolute")}
              >
                Объём
              </button>
            </div>
          )}
          <span className="section-unit">
            {relativeMode ? "% к первому периоду" : unit}
          </span>
        </div>
      }
    >
      {presentation.context && (
        <p className="trend-chart-context">{presentation.context}</p>
      )}
      <div
        className="chart-wrap trend-chart"
        aria-label="Линейный график многолетней динамики"
      >
        <ResponsiveContainer width="100%" height={270}>
          <LineChart
            data={chartRows}
            margin={{ top: 15, right: 28, left: 12, bottom: 8 }}
            onMouseMove={(state) => {
              const period = (
                state?.activePayload?.[0]?.payload as
                  | Record<string, unknown>
                  | undefined
              )?.period_end;
              if (typeof period === "string") setActivePeriod(period);
            }}
          >
            <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
            <XAxis
              dataKey="period"
              tick={<EdgeSafeAxisTick fontSize={11} />}
              axisLine={false}
              tickLine={false}
              tickMargin={8}
              interval="preserveStartEnd"
              padding={{ left: 12, right: 12 }}
            />
            <YAxis
              domain={["auto", "auto"]}
              tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
              tickFormatter={(value) =>
                relativeMode
                  ? `${formatNumber(Number(value))}%`
                  : formatNumber(Number(value))
              }
              axisLine={false}
              tickLine={false}
              width={72}
            />
            <Tooltip
              content={() => null}
              cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }}
            />
            {series.map((item, index) => (
              <Line
                key={item.key}
                name={presentation.names.get(item.key)}
                dataKey={item.key}
                type="monotone"
                connectNulls={false}
                stroke={palette[index % palette.length]}
                strokeWidth={2.5}
                dot={{ r: 3, fill: "var(--surface)", strokeWidth: 2 }}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="trend-series-legend" aria-label="Ряды графика">
        {series.map((item, index) => {
          const name = presentation.names.get(item.key) || item.label;
          return (
            <span className="trend-series-legend-item" key={item.key}>
              <i
                aria-hidden="true"
                style={{ backgroundColor: palette[index % palette.length] }}
              />
              <span title={name}>{name}</span>
            </span>
          );
        })}
      </div>
      {selectedChartRow && selectedAbsoluteRow && (
        <div className="trend-hover-panel" aria-live="polite">
          <div className="trend-hover-heading">
            <span>Период</span>
            <strong>{String(selectedChartRow.period)}</strong>
            <small>Наведите на точку графика для другого периода</small>
          </div>
          <div className="trend-hover-values">
            {series.map((item, index) => {
              const name = presentation.names.get(item.key) || item.label;
              const absolute = Number(selectedAbsoluteRow[item.key]);
              const itemUnit =
                item.unit === "тыс. руб." ? "млрд ₽" : item.unit;
              const change = Number(
                relativeMode
                  ? selectedChartRow[item.key]
                  : item.points.length > 1
                    ? ((Number(
                        item.points.find(
                          (point) =>
                            point.period_end ===
                            selectedChartRow.period_end,
                        )?.value,
                      ) /
                        Number(item.points[0].value)) -
                        1) *
                      100
                    : 0,
              );
              return (
                <div className="trend-hover-value" key={item.key}>
                  <i
                    aria-hidden="true"
                    style={{
                      backgroundColor: palette[index % palette.length],
                    }}
                  />
                  <span title={name}>{name}</span>
                  <strong>
                    {Number.isFinite(absolute)
                      ? `${formatNumber(absolute, {
                          minimumFractionDigits: 2,
                        })} ${itemUnit}`
                      : "—"}
                  </strong>
                  <small>
                    {Number.isFinite(change)
                      ? `${formatNumber(change, {
                          minimumFractionDigits: 2,
                          signDisplay: "always",
                        })}%`
                      : "—"}
                  </small>
                </div>
              );
            })}
          </div>
        </div>
      )}
      <div className="table-scroll trend-data-table">
        <table className="data-table">
          <caption>Табличная альтернатива графику: все точки рядов</caption>
          <thead>
            <tr>
              <th>Ряд</th>
              <th>Период</th>
              <th className="number">Значение</th>
              <th className="number">Изменение от первого года</th>
            </tr>
          </thead>
          <tbody>
            {series.flatMap((item) =>
              item.points.map((point, index) => {
                const divisor =
                  item.unit === "тыс. руб." ? 1_000_000 : 1;
                const first = Number(item.points[0].value);
                return (
                  <tr key={`${item.key}-${point.period_end}`}>
                    {index === 0 && (
                      <td rowSpan={item.points.length}>
                        {presentation.names.get(item.key) || item.label}
                      </td>
                    )}
                    <td>{point.label}</td>
                    <td className="number">
                      {new Intl.NumberFormat("ru-RU", {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      }).format(Number(point.value) / divisor)}{" "}
                      {item.unit === "тыс. руб." ? "млрд ₽" : item.unit}
                    </td>
                    <td className="number">
                      {index === 0
                        ? "—"
                        : new Intl.NumberFormat("ru-RU", {
                            minimumFractionDigits: 2,
                            maximumFractionDigits: 2,
                            signDisplay: "always",
                          }).format((Number(point.value) - first) / divisor)}
                    </td>
                  </tr>
                );
              }),
            )}
          </tbody>
        </table>
      </div>
    </ResultSection>
  );
}

export function LiveFactResults({
  result,
  onShowSources,
}: {
  result: Pick<QueryResult, "facts" | "calculations">;
  onShowSources: () => void;
}) {
  return (
    <details className="result-section collapsible-result-section">
      <summary className="result-section-header">
        <div>
          <span className="section-icon">
            <Database size={17} />
          </span>
          <h2>Подтверждённые факты</h2>
        </div>
        <span className="collapsible-section-action">
          <span className="verified">
            <ShieldCheck size={13} /> Числа проверены
          </span>
          <span className="collapse-prompt">
            <span className="collapsed-label">Показать</span>
            <span className="expanded-label">Скрыть</span>
            <ChevronDown size={14} />
          </span>
        </span>
      </summary>
      <div className="collapsible-section-content">
        <div className="live-fact-grid">
          {result.facts.map((fact) => {
            const display = factDisplayValue(fact);
            return (
              <article className="metric-card live-fact-card" key={fact.fact_id}>
                <div className="metric-top">
                  <span>{fact.metric}</span>
                  <span className="status-chip approved">Раздел {fact.section}</span>
                </div>
                <strong>
                  {display.value} <small>{display.unit}</small>
                </strong>
                <span className="metric-change normal">{fact.period_end}</span>
                <button className="calculation-link" onClick={onShowSources}>
                  {fact.source.formula ||
                    `${fact.source.file} · ${fact.source.sheet} · ${fact.source.cell}`}
                  <ChevronRight size={13} />
                </button>
              </article>
            );
          })}
        </div>
        {result.calculations.length > 0 && (
          <div className="calculation-strip">
            {result.calculations.map((calculation) => (
              <div
                key={`${calculation.series_key || "result"}-${calculation.operation}`}
              >
                <span>
                  {calculation.operation === "difference"
                    ? "Отклонение"
                    : "Темп роста"}
                </span>
                <strong>
                  {calculation.value === null
                    ? "не рассчитывается"
                    : calculation.operation === "difference"
                      ? `${new Intl.NumberFormat("ru-RU", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        }).format(Number(calculation.value) / 1_000_000)} млрд ₽`
                      : `${new Intl.NumberFormat("ru-RU", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        }).format(Number(calculation.value))}%`}
                </strong>
                <small>{calculation.formula}</small>
              </div>
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

function ResultSection({
  icon,
  title,
  action,
  children,
}: {
  icon: ReactNode;
  title: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="result-section">
      <div className="result-section-header">
        <div>
          <span className="section-icon">{icon}</span>
          <h2>{title}</h2>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function SignalBadge({
  signal,
  iconOnly = false,
}: {
  signal: Metric["signal"];
  iconOnly?: boolean;
}) {
  return (
    <span
      className={`signal-badge ${signal} ${iconOnly ? "icon-only" : ""}`}
      title={signalLabels[signal]}
    >
      {signal === "normal" ? (
        <CheckCircle2 size={13} />
      ) : signal === "unknown" ? (
        <Info size={13} />
      ) : (
        <AlertCircle size={13} />
      )}
      {!iconOnly && signalLabels[signal]}
    </span>
  );
}

function RevenueChart() {
  return (
    <div className="chart-wrap" aria-label="График динамики поступлений">
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={chartData} margin={{ top: 15, right: 10, left: -10 }}>
          <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
          <XAxis
            dataKey="period"
            tick={<EdgeSafeAxisTick fontSize={12} />}
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
            padding={{ left: 12, right: 12 }}
          />
          <YAxis
            domain={[44, 56]}
            tick={{ fill: "var(--text-tertiary)", fontSize: 12 }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            contentStyle={{
              background: "var(--surface-elevated)",
              border: "1px solid var(--border)",
              borderRadius: 10,
              color: "var(--text)",
            }}
          />
          <Legend
            iconType="circle"
            wrapperStyle={{ fontSize: 12, color: "var(--text-secondary)" }}
          />
          <Line
            name="Отчётный период"
            dataKey="current"
            type="monotone"
            stroke="var(--accent)"
            strokeWidth={2.5}
            dot={{ r: 3, fill: "var(--surface)", strokeWidth: 2 }}
          />
          <Line
            name="Сравнительный период"
            dataKey="previous"
            type="monotone"
            stroke="var(--chart-muted)"
            strokeWidth={2}
            strokeDasharray="5 5"
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
      <details className="chart-data-details">
        <summary>Показать данные таблицей</summary>
        <table>
          <thead>
            <tr>
              <th>Период</th>
              <th>Отчётный</th>
              <th>Сравнительный</th>
            </tr>
          </thead>
          <tbody>
            {chartData.map((item) => (
              <tr key={item.period}>
                <td>{item.period}</td>
                <td>{item.current}</td>
                <td>{item.previous}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

function RevenueTable({ rows }: { rows: RevenueRow[] }) {
  const [sortAscending, setSortAscending] = useState(false);
  const [filter, setFilter] = useState("");
  const visibleRows = useMemo(
    () =>
      [...rows]
        .filter((row) => row.name.toLowerCase().includes(filter.toLowerCase()))
        .sort((a, b) =>
          sortAscending ? a.change - b.change : b.change - a.change,
        ),
    [rows, sortAscending, filter],
  );

  return (
    <div className="data-table-shell">
      <div className="table-tools">
        <div className="search-field compact-field">
          <Filter size={14} />
          <input
            placeholder="Фильтр"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
        </div>
        <span>{visibleRows.length} строк</span>
      </div>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>Показатель</th>
              <th className="number">I кв. 2026</th>
              <th className="number">I кв. 2025</th>
              <th className="number">
                <button onClick={() => setSortAscending((value) => !value)}>
                  Изменение, % <ArrowDown size={12} />
                </button>
              </th>
              <th className="number">Вклад, млрд ₽</th>
              <th>Статус</th>
              <th aria-label="Действия" />
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((row) => (
              <tr key={row.name}>
                <td>
                  <strong>{row.name}</strong>
                </td>
                <td className="number">{row.current.toFixed(1)}</td>
                <td className="number">{row.previous.toFixed(1)}</td>
                <td
                  className={`number ${row.change < 0 ? "negative" : "positive"}`}
                >
                  {row.change > 0 ? "+" : ""}
                  {row.change.toFixed(1)}%
                </td>
                <td
                  className={`number ${
                    row.contribution < 0 ? "negative" : "positive"
                  }`}
                >
                  {row.contribution > 0 ? "+" : ""}
                  {row.contribution.toFixed(2)}
                </td>
                <td>
                  <SignalBadge signal={row.signal} />
                </td>
                <td>
                  <button className="icon-button small-icon">
                    <ChevronRight size={15} />
                    <span className="sr-only">Открыть строку</span>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PromptComposer({
  value,
  setValue,
  mode,
  setMode,
  streaming,
  onStop,
  onSubmit,
}: {
  value: string;
  setValue: (value: string) => void;
  mode: QueryMode;
  setMode: (mode: QueryMode) => void;
  streaming: boolean;
  onStop: () => void;
  onSubmit: () => void;
}) {
  return (
    <div className="sticky-composer-wrap">
      <div className="sticky-composer">
        <textarea
          rows={2}
          placeholder="Задайте уточняющий вопрос или продолжите анализ…"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
              onSubmit();
            }
          }}
        />
        <div className="composer-toolbar">
          <div>
            <button className="composer-tool" title="Прикрепить файл">
              <Paperclip size={17} />
            </button>
            <select
              className="mode-select"
              value={mode}
              onChange={(event) => setMode(event.target.value as QueryMode)}
              aria-label="Режим запроса"
            >
              <option value="question">Вопрос</option>
              <option value="analysis">Анализ</option>
              <option value="report">Отчёт</option>
            </select>
          </div>
          {streaming ? (
            <button className="stop-button" onClick={onStop}>
              <Square size={13} fill="currentColor" /> Остановить
            </button>
          ) : (
            <button
              className="send-button"
              disabled={!value.trim()}
              onClick={onSubmit}
              aria-label="Отправить запрос"
            >
              <Send size={17} />
            </button>
          )}
        </div>
      </div>
      <small className="composer-disclaimer">
        Проверяйте важные выводы и источники перед утверждением.
      </small>
    </div>
  );
}

export function GeneratorWorkspace({
  context,
  onContextChange,
  onOpenReport,
  initialCommand,
  onInitialCommandConsumed,
}: {
  context: DataContext;
  onContextChange: (context: DataContext) => void;
  onOpenReport: () => void;
  initialCommand?: string;
  onInitialCommandConsumed?: () => void;
}) {
  const [stage, setStage] = useState<"setup" | "processing" | "complete">(
    "setup",
  );
  const [steps, setSteps] = useState<ProcessingStep[]>(
    processingLabels.map((label, index) => ({
      id: index,
      label,
      status: "waiting",
    })),
  );
  const [selectedDataset, setSelectedDataset] = useState("demo-2026-r2");
  const [errorSimulation, setErrorSimulation] = useState(false);
  const [generationError, setGenerationError] = useState("");
  const [catalog, setCatalog] = useState<ReportParameterCatalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [reportMode, setReportMode] = useState<ReportMode>(
    DEMO_MODE ? "national" : "region_vs_rf",
  );
  const [territory, setTerritory] = useState("");
  const [oldPeriodEnd, setOldPeriodEnd] = useState("");
  const [newPeriodEnd, setNewPeriodEnd] = useState("");
  const [command, setCommand] = useState(initialCommand || "");
  const [interpretationStatus, setInterpretationStatus] = useState("");
  const [interpreting, setInterpreting] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const catalogRef = useRef<ReportParameterCatalog | null>(null);

  useEffect(() => {
    if (DEMO_MODE) return;
    const controller = new AbortController();
    backendApi
      .reportParameters(controller.signal)
      .then((nextCatalog) => {
        catalogRef.current = nextCatalog;
        setCatalog(nextCatalog);
        const recommended = nextCatalog.recommended_pair;
        if (recommended) {
          setOldPeriodEnd((current) =>
            current || recommended.old_period_end
          );
          setNewPeriodEnd((current) =>
            current || recommended.new_period_end
          );
        }
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setCatalogError(
          error instanceof Error
            ? error.message
            : "Не удалось загрузить параметры отчёта",
        );
      });
    return () => controller.abort();
  }, []);

  const periodLabel = (periodEnd: string) =>
    (catalogRef.current || catalog)?.periods.find(
      (item) => item.period_end === periodEnd,
    )?.label ||
    periodEnd;

  const applySelectionToContext = (
    nextTerritory: string,
    nextOldPeriod: string,
    nextNewPeriod: string,
  ) => {
    onContextChange({
      ...context,
      territory: nextTerritory || "Российская Федерация",
      reportingPeriod: periodLabel(nextNewPeriod),
      comparisonPeriod: periodLabel(nextOldPeriod),
      dataVersion: "Проверенные книги 1-МСП",
    });
  };

  const interpretCommand = async (sourceCommand = command) => {
    if (!sourceCommand.trim()) return;
    const controller = new AbortController();
    setInterpreting(true);
    setInterpretationStatus("");
    try {
      const specification = await backendApi.interpretReportCommand(
        sourceCommand.trim(),
        controller.signal,
      );
      if (specification.status === "ready") {
        const nextMode = specification.report_mode || "region_vs_rf";
        const nextTerritory = specification.territory || "";
        const nextOld = specification.old_period_end || "";
        const nextNew = specification.new_period_end || "";
        setReportMode(nextMode);
        setTerritory(nextTerritory);
        setOldPeriodEnd(nextOld);
        setNewPeriodEnd(nextNew);
        applySelectionToContext(nextTerritory, nextOld, nextNew);
        setInterpretationStatus(
          "Параметры заполнены. Проверьте их и подтвердите запуск.",
        );
      } else if (specification.status === "clarification") {
        setInterpretationStatus(
          specification.clarification_question ||
            "Уточните параметры стандартной справки.",
        );
      } else {
        setInterpretationStatus(
          specification.no_data_reason || "Для команды нет доступных данных.",
        );
      }
    } catch (error) {
      setInterpretationStatus(
        error instanceof Error
          ? `${error.message}. Заполните форму вручную.`
          : "GNIVC LOCAL недоступен. Заполните форму вручную.",
      );
    } finally {
      setInterpreting(false);
    }
  };

  const initialCommandHandledRef = useRef(false);
  useEffect(() => {
    if (
      DEMO_MODE ||
      !initialCommand ||
      initialCommandHandledRef.current
    ) {
      return;
    }
    initialCommandHandledRef.current = true;
    setCommand(initialCommand);
    onInitialCommandConsumed?.();
    void interpretCommand(initialCommand);
    // The incoming command is deliberately interpreted once per generator visit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialCommand]);

  const effectivePair =
    reportMode === "national" ? catalog?.national_legacy_pair : null;
  const selectedOldPeriod = effectivePair?.old_period_end || oldPeriodEnd;
  const selectedNewPeriod = effectivePair?.new_period_end || newPeriodEnd;
  const generationReady =
    reportMode === "national" ||
    Boolean(territory && selectedOldPeriod && selectedNewPeriod);
  const oldPeriodOptions = Array.from(
    new Map(
      (catalog?.comparable_pairs || []).map((pair) => [
        pair.old_period_end,
        pair.old_label,
      ]),
    ),
  );
  const newPeriodOptions = (catalog?.comparable_pairs || []).filter(
    (pair) => pair.old_period_end === oldPeriodEnd,
  );

  const generate = async () => {
    if (!generationReady) {
      setGenerationError("Выберите регион и сопоставимые периоды");
      return;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    setStage("processing");
    setGenerationError("");
    try {
      const parameters: ReportGenerateParameters = {
        report_mode: reportMode,
        territory:
          reportMode === "region_vs_rf" ? territory : "Российская Федерация",
        old_period_end: selectedOldPeriod || null,
        new_period_end: selectedNewPeriod || null,
      };
      const report = await apiClient.generateReport(
        (nextSteps) => {
          if (errorSimulation && nextSteps[2]?.status === "running") {
            const failed = nextSteps.map((step) =>
              step.id === 2 ? { ...step, status: "error" as const } : step,
            );
            setSteps(failed);
            controller.abort();
            return;
          }
          setSteps(nextSteps);
        },
        controller.signal,
        parameters,
      );
      if (report) {
        sessionStorage.setItem("ai-vector-report-id", String(report.id));
        if (report.download_url) {
          sessionStorage.setItem(
            "ai-vector-report-download",
            report.download_url,
          );
        }
      }
      setStage("complete");
    } catch (error) {
      if (!errorSimulation) {
        setGenerationError(
          error instanceof Error ? error.message : "Ошибка формирования отчёта",
        );
        setStage("setup");
      }
    }
  };

  if (stage === "processing" || stage === "complete") {
    const completedCount = steps.filter((step) => step.status === "done").length;
    const hasError = steps.some((step) => step.status === "error");
    const progress = Math.round((completedCount / steps.length) * 100);

    return (
      <div className="page-workspace generator-progress-page">
        <div className="page-header">
          <div>
            <span className="page-kicker">Формирование отчёта</span>
            <h1>Отчёт 1-МСП за {periodLabel(selectedNewPeriod)}</h1>
            <p>
              {reportMode === "region_vs_rf"
                ? territory
                : "Российская Федерация"}{" "}
              · {DEMO_MODE ? "Демонстрационный набор" : "Проверенные книги"}
            </p>
          </div>
          <span
            className={`status-chip ${
              stage === "complete" ? "approved" : "processing"
            }`}
          >
            {stage === "complete" ? "Проект готов" : "Выполняется"}
          </span>
        </div>

        <div className="generator-progress-layout">
          <section className="progress-card">
            <div className="progress-overview">
              <div
                className="progress-ring"
                style={{ "--progress": `${progress * 3.6}deg` } as React.CSSProperties}
              >
                <span>{progress}%</span>
              </div>
              <div>
                <h2>
                  {stage === "complete"
                    ? "Проект отчёта готов"
                    : hasError
                      ? "Требуется действие"
                      : "Подготавливаем проект отчёта"}
                </h2>
                <p>
                  {stage === "complete"
                    ? "Все этапы выполнены. Проверьте показатели и пояснение."
                    : `${completedCount} из ${steps.length} этапов завершено`}
                </p>
              </div>
            </div>

            <div className="step-list" aria-live="polite">
              {steps.map((step) => (
                <div className={`processing-step ${step.status}`} key={step.id}>
                  <span className="step-state">
                    {step.status === "done" ? (
                      <Check size={15} />
                    ) : step.status === "running" ? (
                      <span className="thinking-spinner small" />
                    ) : step.status === "error" ? (
                      <AlertCircle size={15} />
                    ) : (
                      step.id + 1
                    )}
                  </span>
                  <div>
                    <strong>{step.label}</strong>
                    {step.status === "running" && <small>Выполняется…</small>}
                    {step.status === "done" && <small>Завершено</small>}
                    {step.status === "error" && (
                      <small>
                        Не найдено соответствие для одного кода ОКВЭД
                      </small>
                    )}
                  </div>
                  {step.status === "error" && (
                    <button
                      className="outline-button compact"
                      onClick={() => {
                        setErrorSimulation(false);
                        void generate();
                      }}
                    >
                      <RotateCcw size={13} /> Повторить
                    </button>
                  )}
                </div>
              ))}
            </div>

            <div className="progress-actions">
              {stage === "complete" ? (
                <>
                  <button className="primary-button" onClick={onOpenReport}>
                    Открыть проект отчёта <ArrowRight size={16} />
                  </button>
                  <button
                    className="outline-button"
                    onClick={() => setStage("setup")}
                  >
                    Изменить параметры
                  </button>
                </>
              ) : (
                <button
                  className="outline-button"
                  onClick={() => controllerRef.current?.abort()}
                >
                  <Square size={14} /> Остановить
                </button>
              )}
            </div>
          </section>

          <aside className="run-summary-card">
            <h3>Параметры запуска</h3>
            <dl>
              <dt>Шаблон</dt>
              <dd>Отчёт 1-МСП, v3.4</dd>
              <dt>Территория</dt>
              <dd>
                {reportMode === "region_vs_rf"
                  ? territory
                  : "Российская Федерация"}
              </dd>
              <dt>Режим</dt>
              <dd>
                {reportMode === "region_vs_rf"
                  ? "Регион и РФ"
                  : "Российская Федерация"}
              </dd>
              <dt>Отчётный период</dt>
              <dd>{periodLabel(selectedNewPeriod)}</dd>
              <dt>Сравнение</dt>
              <dd>{periodLabel(selectedOldPeriod)}</dd>
              <dt>Набор данных</dt>
              <dd>
                {DEMO_MODE
                  ? selectedDataset === "demo-2026-r2"
                    ? "2026.07.19-r2"
                    : "2026.07.17-r1"
                  : reportMode === "region_vs_rf"
                    ? "1-МСП, региональные разделы I/II + РФ"
                    : "1-МСП, РФ, контрольная пара"}
              </dd>
            </dl>
            <div className="secure-note">
              <ShieldCheck size={17} />
              <span>
                <strong>Проверяемый расчёт</strong>
                <small>Числа не рассчитываются языковой моделью</small>
              </span>
            </div>
          </aside>
        </div>
      </div>
    );
  }

  return (
    <div className="page-workspace">
      <div className="page-header">
        <div>
          <span className="page-kicker">Новый отчёт</span>
          <h1>Формирование отчёта 1-МСП</h1>
          <p>Расчёт показателей и заполнение утверждённого шаблона Word.</p>
        </div>
        <span className="demo-badge">
          {DEMO_MODE ? "ДЕМО-ДАННЫЕ" : "РЕАЛЬНЫЕ ДАННЫЕ"}
        </span>
      </div>

      <div className="generator-layout">
        <div className="generator-main">
          {!DEMO_MODE && (
            <section className="setup-section report-command-section">
              <div className="setup-section-heading">
                <Sparkles size={20} />
                <div>
                  <h2>Команда для GNIVC LOCAL</h2>
                  <p>
                    Модель только выбирает параметры из каталога. Запуск всегда
                    подтверждается отдельно.
                  </p>
                </div>
              </div>
              <div className="report-command-row">
                <textarea
                  aria-label="Команда стандартной справки"
                  rows={2}
                  placeholder="Например: Собери стандартную справку по 1-МСП по Якутии"
                  value={command}
                  onChange={(event) => setCommand(event.target.value)}
                />
                <button
                  className="outline-button"
                  disabled={!command.trim() || interpreting}
                  onClick={() => void interpretCommand()}
                >
                  {interpreting ? (
                    <span className="thinking-spinner small" />
                  ) : (
                    <WandSparkles size={15} />
                  )}
                  Заполнить форму
                </button>
              </div>
              {interpretationStatus && (
                <div className="secure-note" role="status">
                  <Info size={17} />
                  <span>{interpretationStatus}</span>
                </div>
              )}
            </section>
          )}

          <section className="setup-section">
            <div className="setup-section-heading">
              <span>1</span>
              <div>
                <h2>Шаблон отчёта</h2>
                <p>Утверждённый шаблон определяет структуру документа.</p>
              </div>
            </div>
            <div className="selected-template-card">
              <span className="document-preview-icon">
                <FileText size={25} />
              </span>
              <div>
                <div>
                  <strong>Отчёт 1-МСП</strong>
                  <span className="status-chip approved">Утверждён</span>
                </div>
                <p>Версия 3.4 · Методологическое управление</p>
                <small>Изменён 18.07.2026 · 14 разделов · 12 тегов</small>
              </div>
              <button className="text-button">Изменить</button>
            </div>
          </section>

          <section className="setup-section">
            <div className="setup-section-heading">
              <span>2</span>
              <div>
                <h2>Параметры отчёта</h2>
                <p>Значения будут зафиксированы в версии проекта.</p>
              </div>
            </div>
            <div className="setup-form-grid">
              <label className="select-field">
                <span>Режим отчёта</span>
                <select
                  aria-label="Режим отчёта"
                  value={reportMode}
                  onChange={(event) => {
                    const nextMode = event.target.value as ReportMode;
                    setReportMode(nextMode);
                    const pair =
                      nextMode === "national"
                        ? catalog?.national_legacy_pair
                        : catalog?.recommended_pair;
                    if (pair) {
                      setOldPeriodEnd(pair.old_period_end);
                      setNewPeriodEnd(pair.new_period_end);
                      applySelectionToContext(
                        nextMode === "national"
                          ? "Российская Федерация"
                          : territory,
                        pair.old_period_end,
                        pair.new_period_end,
                      );
                    }
                  }}
                >
                  <option value="region_vs_rf">Регион и РФ</option>
                  <option value="national">Российская Федерация</option>
                </select>
              </label>
              {reportMode === "region_vs_rf" ? (
                <label className="select-field">
                  <span>Территория</span>
                  <select
                    aria-label="Территория отчёта"
                    value={territory}
                    onChange={(event) => {
                      const nextTerritory = event.target.value;
                      setTerritory(nextTerritory);
                      applySelectionToContext(
                        nextTerritory,
                        oldPeriodEnd,
                        newPeriodEnd,
                      );
                    }}
                  >
                    <option value="">Выберите регион</option>
                    {(catalog?.territories || []).map((item) => (
                      <option value={item.name} key={item.name}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <SelectField
                  label="Территория"
                  value="Российская Федерация"
                  onChange={() => undefined}
                  options={["Российская Федерация"]}
                />
              )}
              <label className="select-field">
                <span>Период сравнения</span>
                <select
                  aria-label="Период сравнения отчёта"
                  value={selectedOldPeriod}
                  disabled={reportMode === "national"}
                  onChange={(event) => {
                    const nextOld = event.target.value;
                    const nextPair = catalog?.comparable_pairs.find(
                      (pair) => pair.old_period_end === nextOld,
                    );
                    const nextNew = nextPair?.new_period_end || "";
                    setOldPeriodEnd(nextOld);
                    setNewPeriodEnd(nextNew);
                    applySelectionToContext(territory, nextOld, nextNew);
                  }}
                >
                  {reportMode === "national" && effectivePair ? (
                    <option value={effectivePair.old_period_end}>
                      {effectivePair.old_label}
                    </option>
                  ) : (
                    oldPeriodOptions.map(([value, label]) => (
                      <option value={value} key={value}>
                        {label}
                      </option>
                    ))
                  )}
                </select>
              </label>
              <label className="select-field">
                <span>Отчётный период</span>
                <select
                  aria-label="Отчётный период отчёта"
                  value={selectedNewPeriod}
                  disabled={reportMode === "national"}
                  onChange={(event) => {
                    const nextNew = event.target.value;
                    setNewPeriodEnd(nextNew);
                    applySelectionToContext(
                      territory,
                      oldPeriodEnd,
                      nextNew,
                    );
                  }}
                >
                  {reportMode === "national" && effectivePair ? (
                    <option value={effectivePair.new_period_end}>
                      {effectivePair.new_label}
                    </option>
                  ) : (
                    newPeriodOptions.map((pair) => (
                      <option value={pair.new_period_end} key={pair.new_period_end}>
                        {pair.new_label}
                      </option>
                    ))
                  )}
                </select>
              </label>
              <SelectField
                label="Единицы измерения"
                value="млрд рублей"
                onChange={() => undefined}
                options={["млрд рублей"]}
              />
            </div>
            {reportMode === "region_vs_rf" && (
              <div className="secure-note">
                <ShieldCheck size={17} />
                <span>
                  <strong>Обязательный benchmark: Российская Федерация</strong>
                  <small>
                    Доля региона считается только в поступлениях от МСП по РФ
                  </small>
                </span>
              </div>
            )}
          </section>

          <section className="setup-section">
            <div className="setup-section-heading">
              <span>3</span>
              <div>
                <h2>Набор исходных данных</h2>
                <p>Выберите подготовленный набор или загрузите свои файлы.</p>
              </div>
            </div>
            <div className="dataset-grid">
              {!DEMO_MODE ? (
                <button className="dataset-card selected">
                  <span className="radio-mark"><span /></span>
                  <div>
                    <span className="recommended-label">Контрольная пара</span>
                    <strong>Проверенные книги 1-МСП</strong>
                    <p>
                      {periodLabel(selectedOldPeriod)} и{" "}
                      {periodLabel(selectedNewPeriod)}
                    </p>
                    <small>
                      <CheckCircle2 size={13} /> Разделы I/II и РФ присутствуют
                    </small>
                  </div>
                </button>
              ) : <>
              <button
                className={`dataset-card ${
                  selectedDataset === "demo-2026-r2" ? "selected" : ""
                }`}
                onClick={() => setSelectedDataset("demo-2026-r2")}
              >
                <span className="radio-mark">
                  {selectedDataset === "demo-2026-r2" && <span />}
                </span>
                <div>
                  <span className="recommended-label">Рекомендуемый</span>
                  <strong>Демо-набор 1-МСП</strong>
                  <p>Версия 2026.07.19-r2 · 5 источников</p>
                  <small>
                    <CheckCircle2 size={13} /> Предварительная проверка пройдена
                  </small>
                </div>
              </button>
              <button
                className={`dataset-card ${
                  selectedDataset === "demo-2026-r1" ? "selected" : ""
                }`}
                onClick={() => setSelectedDataset("demo-2026-r1")}
              >
                <span className="radio-mark">
                  {selectedDataset === "demo-2026-r1" && <span />}
                </span>
                <div>
                  <strong>Предыдущая версия</strong>
                  <p>Версия 2026.07.17-r1 · 5 источников</p>
                  <small className="attention-text">
                    <AlertCircle size={13} /> Есть 2 предупреждения
                  </small>
                </div>
              </button>
              <label className="upload-dataset-card">
                <Upload size={21} />
                <strong>Загрузить файлы</strong>
                <small>XLSX, CSV или ZIP · до 250 МБ</small>
                <input type="file" hidden multiple />
              </label>
              </>}
            </div>
          </section>

          {DEMO_MODE && <label className="simulation-toggle">
            <input
              type="checkbox"
              checked={errorSimulation}
              onChange={(event) => setErrorSimulation(event.target.checked)}
            />
            <span>
              <strong>Показать состояние ошибки</strong>
              <small>Остановить демонстрацию на этапе сопоставления кодов</small>
            </span>
          </label>}

          {generationError && (
            <div className="error-message">
              <AlertCircle size={17} />
              Не удалось сформировать отчёт: {generationError}
            </div>
          )}

          {catalogError && (
            <div className="error-message">
              <AlertCircle size={17} />
              Не удалось загрузить каталог: {catalogError}
            </div>
          )}

          <div className="generator-footer">
            <div>
              <CheckCircle2 size={17} />
              <span>
                <strong>
                  {generationReady ? "Готово к запуску" : "Заполните параметры"}
                </strong>
                <small>
                  {generationReady
                    ? "Проверьте итоговые параметры и подтвердите запуск"
                    : "Для регионального отчёта обязателен один регион"}
                </small>
              </span>
            </div>
            <button
              className="primary-button large"
              disabled={!generationReady}
              onClick={() => void generate()}
            >
              <Play size={16} fill="currentColor" /> Сформировать отчёт
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

const REPORT_MATRIX_STREAMS = [
  "01001101DATA1001",
  "MSP2026REPORT01",
  "SUMAVGDELTA100",
  "EVIDENCE010101",
  "REGIONTOPBOTTOM",
  "VERIFY1202601",
  "TABLE010SOURCE",
  "ANALYTICS10110",
  "GROWTHRATE010",
  "FACTS100REPORT",
  "010AGGREGATE11",
  "PERIOD20252026",
  "CHECKSUM01010",
  "METRICVALUE101",
  "STRUCTURE0101",
  "RANKREGION100",
  "SOURCEFACT010",
  "DOCUMENT10101",
  "TREND010TABLE",
  "METHOD0101010",
  "RESULTS100101",
  "VECTOR010101",
];

export function ReportLoadingScreen({ stage }: { stage: string }) {
  return (
    <div
      className="report-loading-screen"
      role="status"
      aria-live="polite"
      aria-label="Формирование аналитического отчёта"
    >
      <div className="report-matrix-rain" aria-hidden="true">
        {REPORT_MATRIX_STREAMS.map((stream, index) => (
          <span
            className="report-matrix-column"
            key={`${stream}-${index}`}
            style={{
              left: `${index * 4.8 - 2}%`,
              animationDelay: `${-(index % 8) * 0.85}s`,
              animationDuration: `${6.2 + (index % 5) * 0.7}s`,
            }}
          >
            {stream.repeat(3).split("").join("\n")}
          </span>
        ))}
      </div>
      <div className="report-matrix-vignette" aria-hidden="true" />
      <div className="report-loading-card">
        <span className="report-loading-kicker">
          АНАЛИТИЧЕСКИЙ ОТЧЁТ // СБОРКА
        </span>
        <div className="report-loading-emblem" aria-hidden="true">
          <FileText size={30} />
          <span />
        </div>
        <h1>Собираем аналитический отчёт</h1>
        <p>{stage}</p>
        <div className="report-loading-progress" aria-hidden="true">
          <span />
        </div>
        <div className="report-loading-steps" aria-hidden="true">
          <span>01 · СТРУКТУРА</span>
          <span>02 · ВЫВОДЫ</span>
          <span>03 · ТАБЛИЦЫ</span>
        </div>
        <small>
          Проверяем связи с источниками. Экран обновится автоматически.
        </small>
      </div>
    </div>
  );
}

function resizeReportTextarea(textarea: HTMLTextAreaElement) {
  textarea.style.height = "auto";
  textarea.style.height = `${textarea.scrollHeight}px`;
}

type SynthesisNotice = {
  tone: "progress" | "success" | "warning" | "error";
  title: string;
  message: string;
};

export function synthesisNoticeFromResult(
  result: Pick<AnalysisSynthesisResult, "used_ai" | "message"> & {
    project: { version: number };
  },
): SynthesisNotice {
  return result.used_ai
    ? {
        tone: "success",
        title: "Отчёт отредактирован в GNIVC LOCAL",
        message: `${result.message} Версия проекта ${result.project.version}.`,
      }
    : {
        tone: "warning",
        title: "ИИ не использован",
        message: `${result.message} Версия проекта ${result.project.version}.`,
      };
}

export function AutoResizeReportText({
  blockId,
  text,
  isHeading = false,
  reportStyle = null,
  editable,
  onChange,
}: {
  blockId: string;
  text: string;
  isHeading?: boolean;
  reportStyle?: string | null;
  editable: boolean;
  onChange: (text: string) => void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    if (textareaRef.current) resizeReportTextarea(textareaRef.current);
  }, [text]);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea || typeof ResizeObserver === "undefined") return;
    let previousWidth = textarea.clientWidth;
    const observer = new ResizeObserver(([entry]) => {
      const nextWidth = entry.contentRect.width;
      if (nextWidth === previousWidth) return;
      previousWidth = nextWidth;
      resizeReportTextarea(textarea);
    });
    observer.observe(textarea);
    return () => observer.disconnect();
  }, []);

  return (
    <textarea
      ref={textareaRef}
      className={[
        "report-text-block",
        isHeading ? "heading" : "",
        reportStyle === "Heading 1" ? "heading-1" : "",
        reportStyle === "Heading 2" ? "heading-2" : "",
        reportStyle === "Heading 3" ? "heading-3" : "",
        blockId === "project-subtitle" ? "subtitle" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      aria-label={`Текстовый блок ${blockId}`}
      value={text}
      rows={1}
      readOnly={!editable}
      onChange={(event) => {
        resizeReportTextarea(event.currentTarget);
        onChange(event.currentTarget.value);
      }}
    />
  );
}

export function ReportChartBlockView({
  block,
}: {
  block: ReportChartBlock;
}) {
  const palette = [
    "#2563eb",
    "#dc2626",
    "#059669",
    "#7c3aed",
    "#d97706",
    "#0891b2",
    "#db2777",
    "#4f46e5",
  ];
  const periodCount = new Set(
    block.series.flatMap((item) =>
      item.points.map((point) => point.period_end),
    ),
  ).size;
  const chartType =
    block.chart_type || (periodCount === 1 ? "bar" : "line");
  const relative =
    chartType === "line" &&
    block.mode === "change" &&
    block.series.length > 1;
  const displayUnit = relative
    ? "% к первому периоду"
    : block.unit === "тыс. руб."
      ? "млрд ₽"
      : block.unit || "Значение";
  const formatValue = (value: number) =>
    new Intl.NumberFormat("ru-RU", {
      maximumFractionDigits: 2,
    }).format(value);
  const rows = useMemo(() => {
    const byPeriod = new Map<string, Record<string, string | number>>();
    block.series.forEach((item) => {
      item.points.forEach((point) => {
        const row = byPeriod.get(point.period_end) || {
          period_end: point.period_end,
          period: point.axis_label || point.label,
        };
        const rawValue = Number(point.value);
        if (Number.isFinite(rawValue)) {
          row[item.key] =
            item.unit === "тыс. руб." ? rawValue / 1_000_000 : rawValue;
        }
        byPeriod.set(point.period_end, row);
      });
    });
    const absoluteRows = [...byPeriod.values()].sort((left, right) =>
      String(left.period_end).localeCompare(String(right.period_end)),
    );
    if (!relative) return absoluteRows;
    const firstValues = new Map<string, number>();
    block.series.forEach((item) => {
      const firstRow = absoluteRows.find(
        (row) => typeof row[item.key] === "number",
      );
      firstValues.set(item.key, Number(firstRow?.[item.key] || 0));
    });
    return absoluteRows.map((row) => {
      const normalized = { ...row };
      block.series.forEach((item) => {
        const current = row[item.key];
        const first = firstValues.get(item.key) || 0;
        if (typeof current === "number" && first !== 0) {
          normalized[item.key] = (current / first - 1) * 100;
        }
      });
      return normalized;
    });
  }, [block, relative]);

  return (
    <figure
      className="report-chart-block"
      aria-label={`График: ${block.title}`}
    >
      <figcaption>
        <div>
          <span>Визуализация</span>
          <h3>{block.title}</h3>
        </div>
        <strong>{displayUnit}</strong>
      </figcaption>
      {block.context && <p>{block.context}</p>}
      <div
        className="report-chart-canvas"
        aria-label={
          chartType === "bar"
            ? "Столбчатая диаграмма одного периода"
            : "Линейный график нескольких периодов"
        }
      >
        {chartType === "bar" ? (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart
              data={rows}
              margin={{ top: 14, right: 28, left: 8, bottom: 12 }}
              barCategoryGap="18%"
            >
              <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
              <XAxis
                dataKey="period"
                tick={<EdgeSafeAxisTick fontSize={9} />}
                axisLine={false}
                tickLine={false}
                tickMargin={8}
                interval="preserveStartEnd"
                padding={{ left: 34, right: 34 }}
              />
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fill: "var(--text-tertiary)", fontSize: 9 }}
                tickFormatter={(value) => formatValue(Number(value))}
                axisLine={false}
                tickLine={false}
                width={62}
              />
              {block.series.map((item, index) => (
                <Bar
                  key={item.key}
                  name={item.label}
                  dataKey={item.key}
                  fill={palette[index % palette.length]}
                  radius={[3, 3, 0, 0]}
                  isAnimationActive={false}
                  maxBarSize={48}
                />
              ))}
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <ResponsiveContainer
            width="100%"
            height={block.period_context ? 290 : 275}
          >
            <LineChart
              data={rows}
              margin={{
                top: 14,
                right: 48,
                left: 10,
                bottom: block.period_context ? 22 : 10,
              }}
            >
              <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
              <XAxis
                dataKey="period"
                tick={<EdgeSafeAxisTick fontSize={9} />}
                axisLine={false}
                tickLine={false}
                tickMargin={8}
                minTickGap={18}
                interval="preserveStartEnd"
                padding={{ left: 38, right: 38 }}
                label={
                  block.period_context
                    ? {
                        value: block.period_context,
                        position: "insideBottom",
                        offset: -10,
                        fill: "var(--text-tertiary)",
                        fontSize: 9,
                      }
                    : undefined
                }
              />
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fill: "var(--text-tertiary)", fontSize: 9 }}
                tickFormatter={(value) =>
                  relative
                    ? `${formatValue(Number(value))}%`
                    : formatValue(Number(value))
                }
                axisLine={false}
                tickLine={false}
                width={62}
              />
              {relative && (
                <ReferenceLine
                  y={0}
                  stroke="var(--border-strong)"
                  strokeDasharray="3 3"
                />
              )}
              {block.series.map((item, index) => (
                <Line
                  key={item.key}
                  name={item.label}
                  dataKey={item.key}
                  type="monotone"
                  connectNulls={false}
                  stroke={palette[index % palette.length]}
                  strokeWidth={2.4}
                  dot={{ r: 2.7, fill: "var(--surface)", strokeWidth: 2 }}
                  activeDot={{ r: 4 }}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>
      {block.series.length > 1 && (
        <div className="report-chart-legend" aria-label="Ряды графика">
          {block.series.map((item, index) => (
            <span key={item.key}>
              <i
                aria-hidden="true"
                style={{ backgroundColor: palette[index % palette.length] }}
              />
              {item.label}
            </span>
          ))}
        </div>
      )}
    </figure>
  );
}

function AnalysisProjectWorkspace({
  conversationId,
  initialProject,
  onBack,
  onToast,
}: {
  conversationId: string;
  initialProject: AnalysisProject | null;
  onBack: () => void;
  onToast: (message: string) => void;
}) {
  const [project, setProject] = useState<AnalysisProject | null>(
    initialProject?.conversation_id === conversationId
      ? initialProject
      : null,
  );
  const [changedBlocks, setChangedBlocks] = useState<Record<string, string>>(
    {},
  );
  const [loadingError, setLoadingError] = useState("");
  const [saving, setSaving] = useState(false);
  const [synthesizing, setSynthesizing] = useState(false);
  const [synthesisNotice, setSynthesisNotice] =
    useState<SynthesisNotice | null>(null);

  useEffect(() => {
    if (initialProject?.conversation_id === conversationId) {
      setProject(initialProject);
      setLoadingError("");
      return;
    }
    let active = true;
    const controller = new AbortController();
    setLoadingError("");
    backendApi
      .conversationProject(conversationId, controller.signal)
      .then((loaded) => {
        if (active) setProject(loaded);
      })
      .catch((error) => {
        if (!active || controller.signal.aborted) return;
        setLoadingError(
          error instanceof Error ? error.message : "Не удалось открыть проект",
        );
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [conversationId, initialProject]);

  const changeText = (blockId: string, text: string) => {
    setProject((current) =>
      current
        ? {
            ...current,
            document: {
              ...current.document,
              blocks: current.document.blocks.map((block) =>
                block.id === blockId && block.type === "paragraph"
                  ? { ...block, text }
                  : block,
              ),
            },
          }
        : current,
    );
    setChangedBlocks((current) => ({ ...current, [blockId]: text }));
  };

  const save = async () => {
    if (!project || Object.keys(changedBlocks).length === 0) return;
    setSaving(true);
    try {
      const updated = await backendApi.updateAnalysisProject(
        project.id,
        project.version,
        changedBlocks,
      );
      setProject(updated);
      setChangedBlocks({});
      onToast(`Версия проекта ${updated.version} сохранена`);
    } catch (error) {
      onToast(
        error instanceof Error ? error.message : "Не удалось сохранить проект",
      );
    } finally {
      setSaving(false);
    }
  };

  const moveProjectBlock = async (blockId: string, offset: number) => {
    if (!project?.blocks) return;
    const ordered = project.blocks.map((block) => block.id);
    const index = ordered.indexOf(blockId);
    const target = index + offset;
    if (index < 0 || target < 0 || target >= ordered.length) return;
    [ordered[index], ordered[target]] = [ordered[target], ordered[index]];
    try {
      const updated = await backendApi.updateAnalysisProject(
        project.id,
        project.version,
        {},
        ordered,
      );
      setProject(updated);
      onToast("Порядок разделов обновлён");
    } catch (error) {
      onToast(
        error instanceof Error ? error.message : "Не удалось изменить порядок",
      );
    }
  };

  const deleteProjectBlock = async (blockId: string) => {
    if (!project) return;
    try {
      const updated = await backendApi.removeProjectBlock(
        conversationId,
        blockId,
      );
      setProject(updated);
      setChangedBlocks({});
      onToast("Результат удалён из проекта");
    } catch (error) {
      onToast(
        error instanceof Error ? error.message : "Не удалось удалить раздел",
      );
    }
  };

  return (
    <div className="report-workspace live-report-workspace">
      <div className="report-toolbar">
        <div className="report-toolbar-left">
          <button className="icon-button" onClick={onBack}>
            <ArrowLeft size={19} />
            <span className="sr-only">Назад</span>
          </button>
          <div>
            <strong>{project?.title || "Аналитический проект"}</strong>
            <span>
              Версия {project?.version || "…"} · проект текущего диалога
            </span>
          </div>
        </div>
        <div className="report-toolbar-actions">
          <button
            className="outline-button"
            disabled={!project || project.blocks_count === 0 || synthesizing}
            onClick={async () => {
              if (!project) return;
              setSynthesizing(true);
              setSynthesisNotice({
                tone: "progress",
                title: "Пересобираем отчёт",
                message:
                  "Проектируем заголовок, структуру, текст и визуализации…",
              });
              try {
                let sourceProject = project;
                if (Object.keys(changedBlocks).length > 0) {
                  setSynthesisNotice({
                    tone: "progress",
                    title: "Сохраняем правки",
                    message:
                      "Сначала сохраняем изменённые текстовые блоки проекта…",
                  });
                  sourceProject = await backendApi.updateAnalysisProject(
                    project.id,
                    project.version,
                    changedBlocks,
                  );
                  setProject(sourceProject);
                  setChangedBlocks({});
                }
                setSynthesisNotice({
                  tone: "progress",
                  title: "Редактируем отчёт в GNIVC LOCAL",
                  message:
                    "GNIVC LOCAL выстраивает структуру и текст, используя факты только из выбранных проверенных результатов…",
                });
                const result =
                  await backendApi.synthesizeAnalysisProject(sourceProject.id);
                setProject(result.project);
                setChangedBlocks({});
                setSynthesisNotice(synthesisNoticeFromResult(result));
                onToast(result.message);
              } catch (error) {
                const message =
                  error instanceof Error
                    ? error.message
                    : "Не удалось обновить итог";
                setSynthesisNotice({
                  tone: "error",
                  title: "Пересборка не выполнена",
                  message,
                });
                onToast(message);
              } finally {
                setSynthesizing(false);
              }
            }}
          >
            <Sparkles size={15} />
            {synthesizing ? "Формируем…" : "Пересобрать с GNIVC LOCAL"}
          </button>
          <button
            className="outline-button"
            disabled={saving || Object.keys(changedBlocks).length === 0}
            onClick={() => void save()}
          >
            {saving ? "Сохранение…" : "Сохранить правки"}
          </button>
          <button
            className="primary-button"
            disabled={!project}
            onClick={() => {
              if (!project) return;
              const link = window.document.createElement("a");
              link.href = `/api/analysis-projects/${project.id}/download/docx`;
              link.download = `Аналитический_проект_v${project.version}.docx`;
              link.click();
            }}
          >
            <Download size={15} /> Word
          </button>
        </div>
      </div>
      {synthesisNotice && (
        <div
          className={`project-synthesis-banner ${synthesisNotice.tone}`}
          role="status"
          aria-live="polite"
        >
          {synthesisNotice.tone === "progress" ? (
            <span className="report-synthesis-spinner" aria-hidden="true" />
          ) : synthesisNotice.tone === "success" ? (
            <CheckCircle2 size={18} />
          ) : (
            <AlertCircle size={18} />
          )}
          <div>
            <strong>{synthesisNotice.title}</strong>
            <span>{synthesisNotice.message}</span>
          </div>
        </div>
      )}
      <div className="report-layout live-report-layout">
        <aside className="report-outline">
          <span className="sidebar-label">Содержание</span>
          <strong>{project?.blocks_count || 0} результатов</strong>
          <span>{project?.document.table_count || 0} таблиц</span>
          <span>{project?.document.chart_count || 0} графиков</span>
          <div className="outline-divider" />
          <div className="project-block-list">
            {(project?.blocks || []).map((block, index, blocks) => (
              <div className="project-block-item" key={block.id}>
                <span>{block.question}</span>
                <div>
                  <button
                    className="icon-button small-icon"
                    disabled={index === 0}
                    onClick={() => void moveProjectBlock(block.id, -1)}
                    title="Выше"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <button
                    className="icon-button small-icon"
                    disabled={index === blocks.length - 1}
                    onClick={() => void moveProjectBlock(block.id, 1)}
                    title="Ниже"
                  >
                    <ChevronRight size={14} />
                  </button>
                  <button
                    className="icon-button small-icon"
                    onClick={() => void deleteProjectBlock(block.id)}
                    title="Удалить из проекта"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))}
          </div>
          <small>
            Тексты редактируются, подтверждённые расчётные таблицы защищены.
          </small>
        </aside>
        <div className="report-document-wrap">
          {loadingError && (
            <div className="error-message">
              <AlertCircle size={17} /> {loadingError}
            </div>
          )}
          {!project && !loadingError && (
            <div className="thinking-state">
              <span className="thinking-spinner" />
              <strong>Открываем проект диалога…</strong>
            </div>
          )}
          {project && (
            <article className="report-document canonical-report-document">
              {project.document.blocks.map((block) =>
                block.type === "table" ? (
                  <div className="report-table-scroll" key={block.id}>
                    <table className="canonical-report-table">
                      <tbody>
                        {block.rows.map((row, rowIndex) => (
                          <tr key={`${block.id}-${rowIndex}`}>
                            {row.map((cell, cellIndex) => {
                              const CellTag = rowIndex === 0 ? "th" : "td";
                              return (
                                <CellTag
                                  key={`${block.id}-${rowIndex}-${cellIndex}`}
                                >
                                  {cell}
                                </CellTag>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : block.type === "chart" ? (
                  <ReportChartBlockView key={block.id} block={block} />
                ) : (
                  <AutoResizeReportText
                    key={block.id}
                    blockId={block.id}
                    text={block.text}
                    isHeading={block.style?.startsWith("Heading")}
                    reportStyle={block.style}
                    editable={block.editable}
                    onChange={(text) => changeText(block.id, text)}
                  />
                ),
              )}
            </article>
          )}
        </div>
        <aside className="review-panel">
          <div className="verification-note">
            <ShieldCheck size={15} />
            Все числовые блоки взяты из сохранённых результатов чата.
          </div>
        </aside>
      </div>
    </div>
  );
}

export function LiveReportWorkspace({
  role,
  status,
  setStatus,
  onBack,
  onToast,
}: {
  role: Role;
  status: ReviewStatus;
  setStatus: (status: ReviewStatus) => void;
  onBack: () => void;
  onToast: (message: string) => void;
}) {
  const reportId = Number(sessionStorage.getItem("ai-vector-report-id"));
  const [documentModel, setDocumentModel] = useState<ReportDocument | null>(
    null,
  );
  const [changedBlocks, setChangedBlocks] = useState<Record<string, string>>(
    {},
  );
  const [loadingError, setLoadingError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!reportId) {
      setLoadingError("Сначала сформируйте отчёт");
      return;
    }
    let active = true;
    const controller = new AbortController();
    setLoadingError("");
    backendApi
      .reportDocument(reportId, controller.signal)
      .then((model) => {
        if (active) {
          setDocumentModel(model);
        }
      })
      .catch((error) => {
        if (!active || controller.signal.aborted) {
          return;
        }
        setLoadingError(
          error instanceof Error ? error.message : "Не удалось открыть отчёт",
        );
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [reportId]);

  const changeText = (blockId: string, text: string) => {
    setDocumentModel((current) =>
      current
        ? {
            ...current,
            blocks: current.blocks.map((block) =>
              block.id === blockId && block.type === "paragraph"
                ? { ...block, text }
                : block,
            ),
          }
        : current,
    );
    setChangedBlocks((current) => ({ ...current, [blockId]: text }));
  };

  const save = async () => {
    if (!documentModel || !reportId || Object.keys(changedBlocks).length === 0) {
      return;
    }
    setSaving(true);
    try {
      const updated = await backendApi.updateReportDocument(
        reportId,
        documentModel.version,
        changedBlocks,
      );
      setDocumentModel(updated);
      setChangedBlocks({});
      onToast(`Версия ${updated.version} сохранена в Web и Word`);
    } catch (error) {
      onToast(
        error instanceof Error ? error.message : "Не удалось сохранить правки",
      );
    } finally {
      setSaving(false);
    }
  };

  const workflow = async (
    action: "submit" | "approve" | "request-revision",
    message: string,
  ) => {
    if (!reportId) return;
    try {
      const updated = await backendApi.reportAction(reportId, action);
      setStatus(updated.status as ReviewStatus);
      onToast(message);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Ошибка статуса");
    }
  };

  const download = () => {
    const url =
      sessionStorage.getItem("ai-vector-report-download") ||
      `/api/reports/${reportId}/download/docx`;
    const link = window.document.createElement("a");
    link.href = url;
    link.download = `Отчёт_1-МСП_версия_${documentModel?.version || 1}.docx`;
    link.click();
  };

  return (
    <div className="report-workspace live-report-workspace">
      <div className="report-toolbar">
        <div className="report-toolbar-left">
          <button className="icon-button" onClick={onBack}>
            <ArrowLeft size={19} />
            <span className="sr-only">Назад</span>
          </button>
          <div>
            <strong>Отчёт 1-МСП</strong>
            <span>
              Версия {documentModel?.version || "…"} · Web и Word синхронизированы
            </span>
          </div>
          <span className={`status-chip ${status}`}>
            {statusLabels[status]}
          </span>
        </div>
        <div className="report-toolbar-actions">
          <button
            className="outline-button"
            disabled={saving || Object.keys(changedBlocks).length === 0}
            onClick={() => void save()}
          >
            {saving ? "Сохранение…" : "Сохранить правки"}
          </button>
          {status === "draft" && (
            <button
              className="primary-button"
              onClick={() =>
                void workflow("submit", "Отчёт отправлен на проверку")
              }
            >
              <Send size={15} /> Отправить на проверку
            </button>
          )}
          {status === "needs_review" && role === "Проверяющий" && (
            <>
              <button
                className="outline-button"
                onClick={() =>
                  void workflow(
                    "request-revision",
                    "Отчёт возвращён на доработку",
                  )
                }
              >
                Вернуть
              </button>
              <button
                className="primary-button"
                onClick={() => void workflow("approve", "Отчёт утверждён")}
              >
                <Check size={15} /> Утвердить
              </button>
            </>
          )}
          <button className="outline-button" onClick={download}>
            <Download size={15} /> Word
          </button>
        </div>
      </div>

      <div className="report-layout live-report-layout">
        <aside className="report-outline">
          <span className="sidebar-label">Документ</span>
          <strong>{documentModel?.paragraph_count || 0} абзацев</strong>
          <span>{documentModel?.table_count || 0} таблицы</span>
          <span>{documentModel?.chart_count || 0} графиков</span>
          <div className="outline-divider" />
          <small>
            Тексты редактируются. Расчётные ячейки таблиц защищены.
          </small>
        </aside>
        <div className="report-document-wrap">
          {loadingError && (
            <div className="error-message">
              <AlertCircle size={17} /> {loadingError}
            </div>
          )}
          {!documentModel && !loadingError && (
            <div className="thinking-state">
              <span className="thinking-spinner" />
              <strong>Загружаем полную версию DOCX…</strong>
            </div>
          )}
          {documentModel && (
            <article className="report-document canonical-report-document">
              {documentModel.blocks.map((block) => {
                if (block.type === "table") {
                  return (
                    <div className="report-table-scroll" key={block.id}>
                      <table className="canonical-report-table">
                        <tbody>
                          {block.rows.map((row, rowIndex) => (
                            <tr key={`${block.id}-${rowIndex}`}>
                              {row.map((cell, cellIndex) => {
                                const CellTag = rowIndex === 0 ? "th" : "td";
                                return (
                                  <CellTag
                                    key={`${block.id}-${rowIndex}-${cellIndex}`}
                                  >
                                    {cell}
                                  </CellTag>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  );
                }
                if (block.type === "chart") {
                  return (
                    <ReportChartBlockView key={block.id} block={block} />
                  );
                }
                const isHeading = block.style?.startsWith("Heading");
                return (
                  <AutoResizeReportText
                    key={block.id}
                    blockId={block.id}
                    text={block.text}
                    isHeading={isHeading}
                    reportStyle={block.style}
                    editable={block.editable}
                    onChange={(text) => changeText(block.id, text)}
                  />
                );
              })}
            </article>
          )}
        </div>
        <aside className="review-panel">
          <div className="review-panel-heading">
            <h3>Проверка</h3>
          </div>
          <div className="verification-note">
            <ShieldCheck size={15} />
            Все таблицы загружены из текущего Word-файла и доступны только для
            чтения.
          </div>
          {Object.keys(changedBlocks).length > 0 && (
            <p>{Object.keys(changedBlocks).length} текстовых блоков изменено</p>
          )}
        </aside>
      </div>
    </div>
  );
}

function ReportWorkspace({
  context,
  role,
  status,
  setStatus,
  onBack,
  onShowInspector,
  onToast,
}: {
  context: DataContext;
  role: Role;
  status: ReviewStatus;
  setStatus: (status: ReviewStatus) => void;
  onBack: () => void;
  onShowInspector: (
    tab: "parameters" | "sources" | "tags" | "warnings",
  ) => void;
  onToast: (message: string) => void;
}) {
  const [aiText, setAiText] = useState(
    DEMO_MODE
      ? "В I квартале 2026 года совокупные поступления по Тульской области составили 48,7 млрд рублей, что на 6,8% ниже уровня сопоставимого периода прошлого года. Основным фактором снижения стало сокращение поступлений налога на прибыль организаций на 3,3 млрд рублей. Наиболее существенное влияние оказали предприятия обрабатывающих производств."
      : "За январь–июнь 2026 года совокупные поступления от субъектов МСП по Российской Федерации составили 7 000,1 млрд рублей против 6 088,9 млрд рублей годом ранее. Темп роста составил 115,0%. Поступления в консолидированный бюджет достигли 5 311,8 млрд рублей, страховые взносы — 1 688,3 млрд рублей. Причины изменения требуют отдельного подтверждённого анализа и не выводятся из агрегатов автоматически.",
  );
  const [section, setSection] = useState("summary");
  const reportMetrics: Metric[] = DEMO_MODE
    ? metrics
    : [
        {
          id: "real-total",
          label: "Поступления от МСП, всего",
          value: "7 000,1 млрд ₽",
          change: "+15,0%",
          signal: "normal",
          sourceId: "report-facts",
        },
        {
          id: "real-budget",
          label: "В консолидированный бюджет",
          value: "5 311,8 млрд ₽",
          change: "+13,3%",
          signal: "normal",
          sourceId: "report-facts",
        },
        {
          id: "real-insurance",
          label: "Страховые взносы",
          value: "1 688,3 млрд ₽",
          change: "+20,5%",
          signal: "normal",
          sourceId: "report-facts",
        },
      ];

  const updateWorkflow = async (
    action: "submit" | "approve" | "request-revision",
    fallbackStatus: ReviewStatus,
    message: string,
  ) => {
    if (!DEMO_MODE) {
      const reportId = Number(sessionStorage.getItem("ai-vector-report-id"));
      if (!reportId) {
        onToast("Сначала сформируйте отчёт");
        return;
      }
      try {
        const updated = await backendApi.reportAction(reportId, action);
        setStatus(updated.status as ReviewStatus);
        onToast(message);
      } catch (workflowError) {
        onToast(
          workflowError instanceof Error
            ? workflowError.message
            : "Не удалось изменить статус",
        );
      }
      return;
    }
    setStatus(fallbackStatus);
    onToast(message);
  };

  const exportFile = (format: "Word" | "Excel" | "PDF") => {
    const realDownload = sessionStorage.getItem("ai-vector-report-download");
    if (!DEMO_MODE && format === "Word" && realDownload) {
      const link = document.createElement("a");
      link.href = realDownload;
      link.download = "Отчет_1-МСП_2025_2026.docx";
      link.click();
      onToast("Проверяемый Word-отчёт загружен");
      return;
    }
    if (!DEMO_MODE) {
      onToast(
        format === "Word"
          ? "Сначала сформируйте отчёт"
          : `Экспорт ${format} не входит в текущий MVP`,
      );
      return;
    }
    const content = `ИИ-ВЕКТОР — демонстрационный отчёт 1-МСП\n${context.territory}\n${context.reportingPeriod}\n\n${aiText}`;
    const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `1-МСП_${context.reportingPeriod.replaceAll(" ", "_")}_${format}.txt`;
    link.click();
    URL.revokeObjectURL(url);
    onToast(`Демонстрационный экспорт ${format} подготовлен`);
  };

  return (
    <div className="report-workspace">
      <div className="report-toolbar">
        <div className="report-toolbar-left">
          <button className="icon-button" onClick={onBack}>
            <ArrowLeft size={19} />
            <span className="sr-only">Назад</span>
          </button>
          <div>
            <strong>Отчёт 1-МСП за январь–июнь 2026</strong>
            <span>Версия 1 · Сохранено только что</span>
          </div>
          <span className={`status-chip ${status}`}>
            {statusLabels[status]}
          </span>
        </div>
        <div className="report-toolbar-actions">
          <button className="outline-button">Предпросмотр</button>
          {status === "draft" && (
            <button
              className="primary-button"
              onClick={() =>
                void updateWorkflow(
                  "submit",
                  "needs_review",
                  "Отчёт отправлен на проверку",
                )
              }
            >
              <Send size={15} /> Отправить на проверку
            </button>
          )}
          {status === "needs_review" && role !== "Проверяющий" && (
            <button className="primary-button" disabled title="Требуется роль Проверяющий">
              <ClipboardCheck size={15} /> Ожидает проверяющего
            </button>
          )}
          {status === "needs_review" && role === "Проверяющий" && (
            <>
              <button
                className="outline-button"
                onClick={() =>
                  void updateWorkflow(
                    "request-revision",
                    "revision_requested",
                    "Отчёт возвращён на доработку",
                  )
                }
              >
                Вернуть на доработку
              </button>
              <button
                className="primary-button"
                onClick={() =>
                  void updateWorkflow(
                    "approve",
                    "approved",
                    "Отчёт утверждён",
                  )
                }
              >
                <Check size={15} /> Утвердить
              </button>
            </>
          )}
          <div className="export-menu">
            <button
              className="outline-button"
              onClick={() => exportFile("Word")}
            >
              <Download size={15} /> Экспорт
              <ChevronDown size={13} />
            </button>
            <div className="export-options">
              <button onClick={() => exportFile("Word")}>Word</button>
              <button onClick={() => exportFile("Excel")}>Excel</button>
              <button onClick={() => exportFile("PDF")}>PDF</button>
            </div>
          </div>
        </div>
      </div>

      <div className="report-layout">
        <aside className="report-outline">
          <span className="sidebar-label">Содержание</span>
          {[
            ["summary", "1. Основные итоги"],
            ["metrics", "2. Ключевые показатели"],
            ["structure", "3. Структура поступлений"],
            ["factors", "4. Факторы изменения"],
            ["appendix", "5. Табличные приложения"],
          ].map(([value, label]) => (
            <button
              key={value}
              className={section === value ? "active" : ""}
              onClick={() => setSection(value)}
            >
              {label}
            </button>
          ))}
          <div className="outline-divider" />
          <button>
            <MessageSquarePlus size={15} /> Комментарии <span>2</span>
          </button>
          <button>
            <Clock3 size={15} /> История версий
          </button>
        </aside>

        <div className="report-document-wrap">
          <article className="report-document">
            <header className="document-header">
              <span>{DEMO_MODE ? "ДЕМОНСТРАЦИОННЫЙ ПРОЕКТ" : "ПРОЕКТ ОТЧЁТА"}</span>
              <h1>Отчёт по форме 1-МСП</h1>
              <p>
                {context.territory} · {context.reportingPeriod}
              </p>
              <div className="document-meta-grid">
                <div>
                  <span>Сравнительный период</span>
                  <strong>{context.comparisonPeriod}</strong>
                </div>
                <div>
                  <span>Версия данных</span>
                  <strong>{context.dataVersion}</strong>
                </div>
                <div>
                  <span>Статус</span>
                  <strong>{statusLabels[status]}</strong>
                </div>
              </div>
            </header>

            <section id="summary">
              <div className="document-section-title">
                <span>01</span>
                <h2>Основные итоги</h2>
              </div>
              <div className="ai-text-block">
                <div className="ai-text-label">
                  <Sparkles size={14} />
                  ПРОЕКТ ТЕКСТА, ПОДГОТОВЛЕННЫЙ ИИ
                  <span>Редактируется</span>
                </div>
                <textarea
                  value={aiText}
                  onChange={(event) => setAiText(event.target.value)}
                  aria-label="Редактируемый пояснительный текст"
                />
                <div className="ai-text-footer">
                  <span>Изменено вами · только что</span>
                  <button className="text-button">
                    <RotateCcw size={13} /> Вернуть версию ИИ
                  </button>
                </div>
              </div>
            </section>

            <section id="metrics">
              <div className="document-section-title">
                <span>02</span>
                <h2>Ключевые показатели</h2>
              </div>
              <div className="document-metrics">
                {reportMetrics.slice(0, 3).map((metric) => (
                  <div key={metric.id}>
                    <span>{metric.label}</span>
                    <strong>{metric.value}</strong>
                    <small className={metric.signal}>{metric.change}</small>
                    <button onClick={() => onShowInspector("sources")}>
                      <Library size={12} /> Источник
                    </button>
                  </div>
                ))}
              </div>
            </section>

            <section id="factors">
              <div className="document-section-title">
                <span>03</span>
                <h2>Факторы изменения</h2>
              </div>
              {DEMO_MODE ? <div className="document-chart">
                <ResponsiveContainer width="100%" height={230}>
                  <BarChart
                    data={revenueRows.slice(0, 5)}
                    layout="vertical"
                    margin={{ left: 20, right: 25 }}
                  >
                    <CartesianGrid
                      stroke="var(--chart-grid)"
                      horizontal={false}
                    />
                    <XAxis
                      type="number"
                      tick={{ fill: "var(--text-tertiary)", fontSize: 11 }}
                      axisLine={false}
                    />
                    <YAxis
                      type="category"
                      dataKey="name"
                      width={125}
                      tick={{ fill: "var(--text-secondary)", fontSize: 11 }}
                      axisLine={false}
                      tickLine={false}
                    />
                    <Tooltip
                      contentStyle={{
                        background: "var(--surface-elevated)",
                        border: "1px solid var(--border)",
                        borderRadius: 10,
                      }}
                    />
                    <Bar dataKey="contribution" radius={4}>
                      {revenueRows.slice(0, 5).map((entry) => (
                        <Cell
                          key={entry.name}
                          fill={
                            entry.contribution < 0
                              ? "var(--danger)"
                              : "var(--success)"
                          }
                        />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div> : (
                <div className="verification-note">
                  <ShieldCheck size={15} />
                  Причинные факторы не выводятся из агрегатов автоматически.
                  Здесь появятся только подтверждённые аналитиком объяснения со
                  ссылкой на источник.
                </div>
              )}
              <p className="document-source">
                <Library size={13} />
                Источник: сводные книги формы 1-МСП за 01.07.2025 и
                01.07.2026 · детерминированный расчёт
              </p>
            </section>

            <div className="document-warning">
              <AlertCircle size={17} />
              <span>
                <strong>Требует проверки человеком</strong>
                Полнота двух источников составляет 97,8%. Ограничение отражено
                в пояснении.
              </span>
            </div>
          </article>
        </div>

        <aside className="review-panel">
          <div className="review-panel-heading">
            <h3>Проверка</h3>
            <span>2 комментария</span>
          </div>
          <article className="comment-card">
            <div>
              <span className="avatar tiny">ЕМ</span>
              <span>
                <strong>Елена М.</strong>
                <small>Сегодня, 11:18</small>
              </span>
            </div>
            <p>
              Проверьте формулировку по обрабатывающим производствам и укажите
              ограничение источника.
            </p>
            <button className="text-button">Ответить</button>
          </article>
          <article className="comment-card resolved">
            <div>
              <CheckCircle2 size={16} />
              <span>
                <strong>Источник показателя</strong>
                <small>Исправлено</small>
              </span>
            </div>
            <p>Ссылка на строку 1010 добавлена.</p>
          </article>
          <div className="review-checklist">
            <h4>Контрольный список</h4>
            {[
              "Источники показателей",
              "Сопоставимость периодов",
              "Пояснение ограничений",
              "Редактура ИИ-текста",
            ].map((item, index) => (
              <label key={item}>
                <input type="checkbox" defaultChecked={index < 3} />
                <span>{item}</span>
              </label>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

type TemplateTagState = {
  code: string;
  label: string;
  state: "valid" | "outdated" | "unknown" | "draft";
  version?: string;
};

function DataWorkspace({ onToast }: { onToast: (message: string) => void }) {
  const [scan, setScan] = useState<DataScanResult | null>(null);
  const [importResult, setImportResult] = useState<ImportResult | null>(null);
  const [periods, setPeriods] = useState<
    {
      period_end: string;
      as_of_date: string | null;
      facts_count: number;
      status: string;
    }[]
  >([]);
  const [phase, setPhase] = useState<"idle" | "scanning" | "importing">("idle");
  const [error, setError] = useState("");

  const refreshPeriods = async () => {
    try {
      setPeriods(await backendApi.periods());
    } catch {
      setPeriods([]);
    }
  };

  useEffect(() => {
    if (!DEMO_MODE) void refreshPeriods();
  }, []);

  const scanFiles = async () => {
    setPhase("scanning");
    setError("");
    try {
      const result = await backendApi.scanData();
      setScan(result);
      onToast(`Распознано файлов: ${result.files_found}`);
    } catch (scanError) {
      setError(
        scanError instanceof Error ? scanError.message : "Ошибка сканирования",
      );
    } finally {
      setPhase("idle");
    }
  };

  const importReady = async () => {
    const readyPaths = scan?.items
      .filter((item) => item.status === "ready")
      .map((item) => item.path);
    if (!readyPaths?.length) return;
    setPhase("importing");
    setError("");
    try {
      const result = await backendApi.importData(readyPaths);
      setImportResult(result);
      await refreshPeriods();
      onToast(`Импорт завершён: новых источников ${result.files_imported}`);
    } catch (importError) {
      setError(
        importError instanceof Error ? importError.message : "Ошибка импорта",
      );
    } finally {
      setPhase("idle");
    }
  };

  return (
    <div className="page-workspace data-workspace">
      <div className="page-header">
        <div>
          <span className="page-kicker">Контур данных</span>
          <h1>Источники формы 1-МСП</h1>
          <p>
            Сканирование локальных книг, контроль схем и импорт фактов с
            воспроизводимым происхождением.
          </p>
        </div>
        <div className="page-header-actions">
          <button
            className="outline-button"
            onClick={() => void scanFiles()}
            disabled={phase !== "idle" || DEMO_MODE}
          >
            {phase === "scanning" ? (
              <span className="thinking-spinner small" />
            ) : (
              <RefreshCw size={15} />
            )}
            Сканировать каталог
          </button>
          <button
            className="primary-button"
            onClick={() => void importReady()}
            disabled={!scan?.ready || phase !== "idle" || DEMO_MODE}
          >
            {phase === "importing" ? (
              <span className="thinking-spinner small" />
            ) : (
              <Upload size={15} />
            )}
            Импортировать готовые
          </button>
        </div>
      </div>

      {DEMO_MODE && (
        <div className="warning-inline">
          <Info size={17} />
          <div>
            <strong>Импорт недоступен в демо-режиме</strong>
            <p>Запустите приложение без VITE_DEMO_MODE для работы с книгами.</p>
          </div>
        </div>
      )}
      {error && (
        <div className="error-message">
          <AlertCircle size={17} />
          {error}
        </div>
      )}

      <div className="data-summary-grid">
        {[
          ["Файлов найдено", scan?.files_found ?? "—"],
          ["Готово к импорту", scan?.ready ?? "—"],
          ["Карантин", scan?.quarantine ?? "—"],
          ["Периодов в БД", periods.length],
        ].map(([label, value]) => (
          <article className="data-stat-card" key={label}>
            <span>{label}</span>
            <strong>{value}</strong>
          </article>
        ))}
      </div>

      {importResult && (
        <section className="data-status-card">
          <CheckCircle2 size={18} />
          <div>
            <strong>Импорт #{importResult.id}: {importResult.status}</strong>
            <p>
              Найдено {importResult.files_found}; новых источников{" "}
              {importResult.files_imported}; карантин{" "}
              {importResult.files_quarantined}; ошибок{" "}
              {importResult.errors_count}.
            </p>
          </div>
        </section>
      )}

      <div className="data-layout">
        <section className="data-table-card">
          <div className="result-section-header">
            <div>
              <span className="section-icon">
                <Database size={17} />
              </span>
              <h2>Распознанные книги</h2>
            </div>
            {scan && <span className="section-unit">{scan.root}</span>}
          </div>
          {!scan ? (
            <div className="empty-data-state">
              <Database size={28} />
              <strong>Каталог ещё не просканирован</strong>
              <p>Сканирование читает структуру книг, но не изменяет данные.</p>
            </div>
          ) : (
            <div className="table-scroll">
              <table className="data-file-table">
                <thead>
                  <tr>
                    <th>Файл</th>
                    <th>Тип</th>
                    <th>Дата</th>
                    <th>Граф</th>
                    <th>Статус</th>
                  </tr>
                </thead>
                <tbody>
                  {scan.items.slice(0, 80).map((item) => (
                    <tr key={item.path}>
                      <td title={item.path}>{item.name}</td>
                      <td>{item.workbook_kind}</td>
                      <td>{item.as_of_date || "—"}</td>
                      <td>{item.numeric_columns ?? "—"}</td>
                      <td>
                        <span className={`source-status ${item.status}`}>
                          {item.status === "ready"
                            ? "Готов"
                            : item.status === "ignored"
                              ? "Служебный"
                              : "Карантин"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {scan.items.length > 80 && (
                <p className="table-footnote">
                  Показаны первые 80 из {scan.items.length} файлов.
                </p>
              )}
            </div>
          )}
        </section>

        <aside className="periods-card">
          <h3>Доступные периоды</h3>
          {periods.length === 0 ? (
            <p>После импорта здесь появятся периоды и число фактов.</p>
          ) : (
            periods.map((period) => (
              <div className="period-row" key={`${period.period_end}-${period.status}`}>
                <span>
                  <strong>{period.period_end}</strong>
                  <small>{period.status}</small>
                </span>
                <b>{period.facts_count.toLocaleString("ru-RU")} фактов</b>
              </div>
            ))
          )}
          <div className="secure-note">
            <ShieldCheck size={17} />
            <span>
              <strong>Аудируемый импорт</strong>
              <small>SHA-256, схема, лист, ячейка и исходное значение</small>
            </span>
          </div>
        </aside>
      </div>
    </div>
  );
}

function TemplateWorkspace({ onToast }: { onToast: (message: string) => void }) {
  const [mode, setMode] = useState<"library" | "validation">("library");
  const [fileName, setFileName] = useState("");
  const [tagStates, setTagStates] = useState<TemplateTagState[]>([
    {
      code: "total_revenue",
      label: "Поступления, всего",
      state: "valid",
      version: "3.1",
    },
    {
      code: "revenue_growth",
      label: "Темп роста",
      state: "outdated",
      version: "1.0",
    },
    {
      code: "tax_risk_comment",
      label: "Комментарий к риску",
      state: "unknown",
    },
  ]);

  const openDemo = (name = "Шаблон_1-МСП_проект.docx") => {
    setFileName(name);
    setMode("validation");
  };

  if (mode === "validation") {
    const allReady = tagStates.every(
      (item) => item.state === "valid" || item.state === "draft",
    );
    return (
      <div className="page-workspace template-validation-page">
        <div className="page-header">
          <div>
            <button className="back-link" onClick={() => setMode("library")}>
              <ArrowLeft size={14} /> К шаблонам
            </button>
            <h1>Проверка Word-шаблона</h1>
            <p>Сопоставьте найденные плейсхолдеры со справочником тегов.</p>
          </div>
          <span className={`status-chip ${allReady ? "approved" : "needs_review"}`}>
            {allReady ? "Готов к сохранению" : "Требует исправлений"}
          </span>
        </div>

        <div className="validation-layout">
          <section className="word-preview">
            <div className="word-file-bar">
              <span className="word-icon">W</span>
              <div>
                <strong>{fileName}</strong>
                <small>Демонстрационный документ · 84 КБ</small>
              </div>
              <button className="outline-button compact">
                <Upload size={13} /> Заменить
              </button>
            </div>
            <article className="paper-preview">
              <span className="paper-demo-label">ПРЕДВАРИТЕЛЬНЫЙ ПРОСМОТР</span>
              <h2>Аналитическая справка</h2>
              <p className="centered">
                о поступлениях за{" "}
                <mark className="tag-valid">{`{{reporting_period}}`}</mark>
              </p>
              <p>
                За отчётный период поступления по региону составили{" "}
                <mark className="tag-valid">{`{{total_revenue}}`}</mark>, что на{" "}
                <mark
                  className={
                    tagStates[1].state === "valid"
                      ? "tag-valid"
                      : "tag-outdated"
                  }
                >{`{{revenue_growth}}`}</mark>{" "}
                отличается от значения сравнительного периода.
              </p>
              <p>
                Основной риск:{" "}
                <mark
                  className={
                    tagStates[2].state === "draft"
                      ? "tag-draft"
                      : "tag-unknown"
                  }
                >{`{{tax_risk_comment}}`}</mark>
              </p>
              <mark className="tag-valid block-tag">{`{{table_top_okved}}`}</mark>
              <div className="fake-table">
                <span />
                <span />
                <span />
                <span />
                <span />
                <span />
              </div>
            </article>
          </section>

          <section className="validation-panel">
            <div className="validation-summary">
              <div>
                <span className="summary-number">5</span>
                <small>Найдено</small>
              </div>
              <div>
                <span className="summary-number success-text">3</span>
                <small>Корректны</small>
              </div>
              <div>
                <span className="summary-number attention-text">
                  {tagStates.filter((item) => item.state === "outdated").length}
                </span>
                <small>Устарели</small>
              </div>
              <div>
                <span className="summary-number danger-text">
                  {tagStates.filter((item) => item.state === "unknown").length}
                </span>
                <small>Неизвестны</small>
              </div>
            </div>

            <div className="validation-list">
              {tagStates.map((item) => (
                <article className={`validation-item ${item.state}`} key={item.code}>
                  <span className="validation-state-icon">
                    {item.state === "valid" ? (
                      <Check size={15} />
                    ) : item.state === "draft" ? (
                      <FilePlus2 size={15} />
                    ) : (
                      <AlertCircle size={15} />
                    )}
                  </span>
                  <div>
                    <code>{`{{${item.code}}}`}</code>
                    <strong>{item.label}</strong>
                    <small>
                      {item.state === "valid" && `Корректен · v${item.version}`}
                      {item.state === "outdated" &&
                        `Устарел · v${item.version}. Доступна версия 2.4`}
                      {item.state === "unknown" &&
                        "Тег отсутствует в справочнике"}
                      {item.state === "draft" &&
                        "Создан черновик · требуется согласование"}
                    </small>
                  </div>
                  {item.state === "outdated" && (
                    <button
                      className="outline-button compact"
                      onClick={() =>
                        setTagStates((current) =>
                          current.map((tagItem) =>
                            tagItem.code === item.code
                              ? {
                                  ...tagItem,
                                  code: "revenue_growth_percent",
                                  label: "Изменение поступлений",
                                  state: "valid",
                                  version: "2.4",
                                }
                              : tagItem,
                          ),
                        )
                      }
                    >
                      Заменить
                    </button>
                  )}
                  {item.state === "unknown" && (
                    <button
                      className="outline-button compact"
                      onClick={() => {
                        setTagStates((current) =>
                          current.map((tagItem) =>
                            tagItem.code === item.code
                              ? { ...tagItem, state: "draft", version: "0.1" }
                              : tagItem,
                          ),
                        );
                        onToast("Черновик тега создан");
                      }}
                    >
                      Создать тег
                    </button>
                  )}
                </article>
              ))}
            </div>

            <div className="validation-note">
              <ShieldCheck size={17} />
              <p>
                Правила тегов выполняются на сервере. Произвольный SQL в браузере
                не используется.
              </p>
            </div>

            <div className="validation-actions">
              <button
                className="outline-button"
                onClick={() => onToast("Повторная проверка завершена")}
              >
                <RefreshCw size={15} /> Проверить повторно
              </button>
              <button
                className="primary-button"
                disabled={!allReady}
                onClick={() => onToast("Шаблон сохранён как черновик")}
              >
                <Check size={15} /> Сохранить шаблон
              </button>
            </div>
          </section>
        </div>
      </div>
    );
  }

  return (
    <div className="page-workspace">
      <div className="page-header">
        <div>
          <span className="page-kicker">Документы</span>
          <h1>Шаблоны отчётов</h1>
          <p>Создавайте документы на основе утверждённых форм и тегов.</p>
        </div>
        <div className="page-header-actions">
          <label className="outline-button">
            <Upload size={15} /> Загрузить Word-шаблон
            <input
              type="file"
              accept=".doc,.docx"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) openDemo(file.name);
              }}
            />
          </label>
          <button className="primary-button">
            <Plus size={15} /> Создать шаблон
          </button>
        </div>
      </div>

      <div className="template-library-tools">
        <div className="search-field">
          <Search size={16} />
          <input placeholder="Найти шаблон" />
        </div>
        <button className="filter-button active">Все</button>
        <button className="filter-button">Утверждённые</button>
        <button className="filter-button">Мои шаблоны</button>
        <button className="filter-button">Черновики</button>
      </div>

      <section className="template-section">
        <div className="section-title-row">
          <h2>Часто используемые</h2>
          <span>2 шаблона</span>
        </div>
        <div className="template-card-grid">
          {templates
            .filter((template) => template.frequent)
            .map((template) => (
              <TemplateCard
                key={template.id}
                template={template}
                featured
                onOpen={() => openDemo()}
              />
            ))}
        </div>
      </section>

      <section className="template-section">
        <div className="section-title-row">
          <h2>Все шаблоны</h2>
          <span>{templates.length} шаблонов</span>
        </div>
        <div className="template-card-grid">
          {templates.map((template) => (
            <TemplateCard
              key={template.id}
              template={template}
              onOpen={() => openDemo()}
            />
          ))}
        </div>
      </section>
    </div>
  );
}

function TemplateCard({
  template,
  featured = false,
  onOpen,
}: {
  template: (typeof templates)[number];
  featured?: boolean;
  onOpen: () => void;
}) {
  return (
    <article className={`template-card ${featured ? "featured" : ""}`}>
      <div className="template-card-top">
        <span className="template-document-icon">
          <FileText size={22} />
        </span>
        <span className={`status-chip ${template.status}`}>
          {template.status === "approved"
            ? "Утверждён"
            : template.status === "review"
              ? "На согласовании"
              : "Черновик"}
        </span>
        <button className="icon-button small-icon">
          <MoreHorizontal size={17} />
        </button>
      </div>
      <h3>{template.name}</h3>
      <p>{template.purpose}</p>
      <dl>
        <dt>Владелец</dt>
        <dd>{template.owner}</dd>
        <dt>Версия</dt>
        <dd>{template.version}</dd>
        <dt>Изменён</dt>
        <dd>{template.updatedAt}</dd>
      </dl>
      <div className="template-card-actions">
        <button className="primary-button compact" onClick={onOpen}>
          Использовать <ArrowRight size={14} />
        </button>
        <button className="outline-button compact">Открыть</button>
      </div>
    </article>
  );
}

function KnowledgeWorkspace({ onToast }: { onToast: (message: string) => void }) {
  const documents = [
    {
      type: "Методика",
      title: "Порядок формирования показателей формы 1-МСП",
      version: "Редакция 4.2",
      date: "Действует с 01.01.2026",
      status: "Актуален",
    },
    {
      type: "Справочник",
      title: "Общероссийский классификатор видов деятельности",
      version: "ОКВЭД 2",
      date: "Обновлён 10.01.2026",
      status: "Актуален",
    },
    {
      type: "Инструкция",
      title: "Правила сопоставления периодов статистической отчётности",
      version: "Версия 2.7",
      date: "Действует с 01.04.2025",
      status: "Актуален",
    },
    {
      type: "Методика",
      title: "Расчёт факторов изменения налоговых поступлений",
      version: "Редакция 3.0",
      date: "Требует пересмотра",
      status: "Устаревает",
    },
  ];
  return (
    <div className="page-workspace">
      <div className="page-header">
        <div>
          <span className="page-kicker">Методические материалы</span>
          <h1>База знаний</h1>
          <p>Проверенные документы, правила расчёта и справочники.</p>
        </div>
        <button
          className="primary-button"
          onClick={() => onToast("Демонстрационная загрузка открыта")}
        >
          <Upload size={15} /> Добавить документ
        </button>
      </div>
      <div className="knowledge-search">
        <Search size={19} />
        <input placeholder="Найти методику, показатель или фрагмент документа" />
        <button className="primary-button">Найти</button>
      </div>
      <div className="knowledge-layout">
        <aside className="knowledge-filters">
          <h3>Фильтры</h3>
          {["Все документы", "Методики", "Справочники", "Инструкции", "Примеры"].map(
            (item, index) => (
              <button className={index === 0 ? "active" : ""} key={item}>
                {item}
                <span>{index === 0 ? 24 : index * 3 + 2}</span>
              </button>
            ),
          )}
        </aside>
        <section className="knowledge-results">
          <div className="section-title-row">
            <h2>Рекомендуемые документы</h2>
            <span>4 документа</span>
          </div>
          {documents.map((document) => (
            <article className="knowledge-card" key={document.title}>
              <span className="file-icon">
                <BookOpen size={17} />
              </span>
              <div>
                <span className="document-type">{document.type}</span>
                <h3>{document.title}</h3>
                <p>
                  {document.version} · {document.date}
                </p>
                <small>Связано с формой 1-МСП и 6 показателями</small>
              </div>
              <span
                className={`knowledge-status ${
                  document.status === "Устаревает" ? "attention" : ""
                }`}
              >
                {document.status}
              </span>
              <button className="outline-button compact">Открыть</button>
            </article>
          ))}
        </section>
      </div>
    </div>
  );
}

function JobsWorkspace({ onToast }: { onToast: (message: string) => void }) {
  const jobs = [
    {
      name: "Отчёт 1-МСП — квартальный",
      schedule: "После обновления набора 1-МСП",
      next: "Ожидает данные за II квартал",
      status: "active",
    },
    {
      name: "Мониторинг неблагоприятных показателей",
      schedule: "Ежемесячно, 5 числа в 07:00",
      next: "05.08.2026, 07:00",
      status: "active",
    },
    {
      name: "Сравнение со средним по РФ",
      schedule: "Ежеквартально",
      next: "01.10.2026, 08:30",
      status: "paused",
    },
  ];
  return (
    <div className="page-workspace">
      <div className="page-header">
        <div>
          <span className="page-kicker">Автоматизация</span>
          <h1>Регулярные задания</h1>
          <p>Запуск отчётов по расписанию или после обновления данных.</p>
        </div>
        <button
          className="primary-button"
          onClick={() => onToast("Мастер задания доступен в демо-режиме")}
        >
          <Plus size={15} /> Создать задание
        </button>
      </div>
      <div className="job-summary-grid">
        <div>
          <span>Активные задания</span>
          <strong>2</strong>
          <small>Следующий запуск через 8 дней</small>
        </div>
        <div>
          <span>Успешно за 30 дней</span>
          <strong>12</strong>
          <small className="success-text">100% выполнено</small>
        </div>
        <div>
          <span>Требуют внимания</span>
          <strong>1</strong>
          <small className="attention-text">Ожидает данные</small>
        </div>
      </div>
      <section className="jobs-list">
        <div className="section-title-row">
          <h2>Задания</h2>
          <button className="outline-button compact">
            <Filter size={14} /> Фильтр
          </button>
        </div>
        {jobs.map((job) => (
          <article className="job-card" key={job.name}>
            <span className={`job-icon ${job.status}`}>
              <Clock3 size={18} />
            </span>
            <div>
              <h3>{job.name}</h3>
              <p>{job.schedule}</p>
            </div>
            <div>
              <span>Следующий запуск</span>
              <strong>{job.next}</strong>
            </div>
            <span className={`status-chip ${job.status === "active" ? "approved" : "draft"}`}>
              {job.status === "active" ? "Активно" : "Приостановлено"}
            </span>
            <button className="icon-button">
              <MoreHorizontal size={18} />
            </button>
          </article>
        ))}
      </section>
    </div>
  );
}
