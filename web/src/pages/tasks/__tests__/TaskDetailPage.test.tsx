/**
 * `TaskDetailPage` smoke test.
 *
 * Mocks the two data sources independently so the test can exercise
 * the page's three render branches in isolation:
 *   * Loading — neither hook has resolved.
 *   * Running task with live SSE frames — assert the header, progress,
 *     and a frame from the event log are rendered.
 *   * Finished task with a result summary — assert the summary card
 *     surfaces the row counts.
 *
 * Why module-mock both `@/lib/api/tasks` (REST) and
 * `@/lib/api/sse/run-task-stream` (SSE): the page composes the two
 * channels — polling for the canonical row + streaming for the live
 * event log — and the unit test should not actually open a network
 * connection or a real SSE channel. The list-page test (T14) follows
 * the same module-mock pattern.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router";
import { I18nextProvider, initReactI18next } from "react-i18next";
import i18n from "i18next";

import en from "@/lib/i18n/locales/en.json";
import zh from "@/lib/i18n/locales/zh.json";
import type { ResourceAction } from "@/lib/api/organizations";
import type { ScheduledRunExecution, Task, TaskStatus } from "@/lib/api/tasks";

vi.mock("@/lib/api/tasks", () => ({
  getTask: vi.fn(),
  getTaskEvents: vi.fn(),
  cancelTask: vi.fn(),
  resumeTask: vi.fn(),
  getScheduledRun: vi.fn(),
  listScheduledRunExecutions: vi.fn(),
  runScheduledNow: vi.fn(),
  pauseScheduledRun: vi.fn(),
  resumeScheduledRun: vi.fn(),
  cancelScheduledExecution: vi.fn(),
}));

vi.mock("@/lib/api/sse/run-task-stream", () => ({
  useTaskStream: vi.fn(),
}));

import {
  getScheduledRun,
  getTask,
  getTaskEvents,
  listScheduledRunExecutions,
} from "@/lib/api/tasks";
import { useTaskStream } from "@/lib/api/sse/run-task-stream";
import { TaskDetailPage } from "@/pages/tasks/TaskDetailPage";

const TASK_ID = "00000000-0000-0000-0000-000000000abc";

const testI18n = i18n.createInstance();
void testI18n.use(initReactI18next).init({
  lng: "en",
  fallbackLng: "en",
  resources: {
    en: { translation: en },
    zh: { translation: zh },
  },
  interpolation: { escapeValue: false },
});

function renderAt(taskId: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={testI18n}>
        <MemoryRouter initialEntries={[`/tasks/${taskId}`]}>
          <Routes>
            <Route path="/tasks/:taskId" element={<TaskDetailPage />} />
          </Routes>
        </MemoryRouter>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

function makeTask(over: Partial<{
  status: TaskStatus;
  progress: number;
  result: unknown;
  results_uri: string | null;
  error: string | null;
  finished_at: string | null;
}> = {}): Task {
  return {
    id: TASK_ID,
    status: "running" as TaskStatus,
    progress: 0.42,
    task_type: "batch_exec",
    workflow_id: "wf_42",
    payload: {},
    result: null,
    results_uri: null,
    error: null,
    background_job_id: TASK_ID,
    submitted_at: "2026-05-24T10:00:00Z",
    started_at: "2026-05-24T10:00:01Z",
    finished_at: null,
    access: { capabilities: ['view', 'export', 'update', 'delete', 'manage_access', 'execute', 'cancel', 'resume', 'inspect_runs'], effective_role: 'manager', source: 'computed' },
    provenance: {
      ownership_scope: 'personal',
      origin_type: 'created',
      owner: { type: 'user', display_name: 'Task owner' },
      created_by: { type: 'user', display_name: 'Task owner' },
    },
    ...over,
  };
}

function withCapabilities(task: Task, capabilities: ResourceAction[]): Task {
  return {
    ...task,
    access: {
      capabilities,
      effective_role: "viewer",
      source: "computed",
    },
  };
}

describe("<TaskDetailPage>", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await testI18n.changeLanguage("en");
    vi.mocked(useTaskStream).mockReturnValue({ events: [], done: false });
    vi.mocked(getTaskEvents).mockResolvedValue({
      items: [],
      limit: 50,
      after_seq: null,
      before_seq: null,
      order: "desc",
      next_cursor: null,
      latest_seq: 0,
    });
  });

  it("renders the workflow id, status badge, and progress for a running task", async () => {
    vi.mocked(getTask).mockResolvedValue(makeTask());
    vi.mocked(useTaskStream).mockReturnValue({
      events: [
        {
          id: 1,
          event_type: "progress",
          payload: { progress: { done: 1, total: 2 } },
        },
      ],
      done: false,
    });

    renderAt(TASK_ID);

    // workflow id from the task row
    await waitFor(() => {
      expect(screen.getByText(/wf_42/)).toBeInTheDocument();
    });
    // progress (Math.round(0.42 * 100) = 42)
    expect(screen.getByText("42%")).toBeInTheDocument();
    // plain-language row progress derived from the live `progress` SSE frame
    // ({done:1, total:2}) so a non-technical user reads it at a glance.
    expect(screen.getByTestId("row-progress")).toHaveTextContent(
      "1 of 2 rows done",
    );
    // status badge — the page uses `tasks.status.running` with default
    // 'running' as the fallback string
    expect(screen.getByText("Running")).toBeInTheDocument();
    // event row — the mocked SSE frame renders its event_type alongside filters.
    expect(screen.getAllByText("Progress").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Share task" })).toBeInTheDocument();
    await waitFor(() => {
      expect(getTaskEvents).toHaveBeenCalledWith(TASK_ID, expect.objectContaining({
        limit: 50,
        order: "desc",
      }));
    });
  });

  it("shows cleanup and interrupted states as Cancelled without changing progress", async () => {
    vi.mocked(getTask).mockResolvedValue(
      makeTask({ status: "cancelling", progress: 0.42 }),
    );

    renderAt(TASK_ID);

    await waitFor(() => {
      expect(screen.getByText("Cancelled")).toBeInTheDocument();
    });
    expect(screen.getByText("42%")).toBeInTheDocument();
    expect(screen.queryByText("Cancelling")).not.toBeInTheDocument();
  });

  it("renders the summary card when the task has finished with a result", async () => {
    const completed = makeTask({
        status: "finished",
        progress: 1,
        result: { rows_total: 10, rows_ok: 8, rows_failed: 2 },
        results_uri: "memory://tasks/abc/results.csv",
        finished_at: "2026-05-24T10:05:00Z",
      });
    completed.payload = {
      data_source: { rows: [{ email: "a@example.com" }, { email: "b@example.com" }] },
      column_mapping: { email: "start.email" },
      concurrency: 2,
      output: { type: "vfs_data", path: "/reports/results.xlsx" },
    };
    vi.mocked(getTask).mockResolvedValue(completed);

    renderAt(TASK_ID);

    await waitFor(() => {
      expect(screen.getByText("Summary")).toBeInTheDocument();
    });
    // The three summary counters (rows_total / rows_ok / rows_failed)
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
    expect(screen.getByText("Task setup")).toBeInTheDocument();
    expect(screen.getByText("/reports/results.xlsx")).toBeInTheDocument();
    // Download link → backend redirect endpoint
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view in storage/i })).toHaveAttribute(
      "href",
      `/storage?path=${encodeURIComponent(`/task/${TASK_ID}`)}`,
    );
  });

  it("shows the load-error fallback when the task cannot be fetched", async () => {
    vi.mocked(getTask).mockRejectedValue(new Error("404"));

    renderAt(TASK_ID);

    await waitFor(() => {
      expect(
        screen.getByText(
          /failed to load this task/i,
        ),
      ).toBeInTheDocument();
    });
  });

  it("keeps lifecycle actions permission-aware and shows a clear empty event state", async () => {
    const user = userEvent.setup();
    vi.mocked(getTask).mockResolvedValue(withCapabilities(makeTask(), ["view"]));

    renderAt(TASK_ID);

    await screen.findByRole("tab", { name: "Execution logs" });
    await user.click(screen.getByRole("tab", { name: "Execution logs" }));
    expect(await screen.findByText("No events yet.")).toBeInTheDocument();
    expect(document.querySelector('[data-role="task-event-log-scroll-region"]')).toHaveClass(
      "min-h-0",
      "flex-1",
      "overflow-y-auto",
      "overscroll-contain",
    );
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Share task" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument();
  });

  it("presents a failed resumable task with a concise error and recovery action", async () => {
    vi.mocked(getTask).mockResolvedValue(makeTask({
      status: "failed",
      error: JSON.stringify({ message: "The source file has no header row." }),
      result: { artifact_uris: { jsonl: "memory://tasks/abc/partial.jsonl" } },
    }));

    renderAt(TASK_ID);

    expect(await screen.findByText("The source file has no header row.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it("uses localized dropdown filters and narrows the event list", async () => {
    const user = userEvent.setup();
    vi.mocked(getTask).mockResolvedValue(makeTask());
    vi.mocked(useTaskStream).mockReturnValue({
      events: [
        { id: 1, event_type: "progress", payload: { message: "Row one", progress: { done: 1, total: 2 } } },
        { id: 2, event_type: "log", payload: { level: "warning", message: "Needs review" } },
      ],
      done: false,
    });

    renderAt(TASK_ID);

    await screen.findByRole("tab", { name: "Execution logs" });
    await user.click(screen.getByRole("tab", { name: "Execution logs" }));
    await screen.findByText("2 visible · 2 loaded");
    expect(screen.getByRole("combobox", { name: "Severity" })).toBeInTheDocument();
    expect(screen.getByTestId("task-event-filters").firstElementChild).toHaveClass("flex-wrap");
    const eventType = screen.getByRole("combobox", { name: "Event type" });
    await user.click(eventType);
    await user.click(screen.getByRole("option", { name: "Log" }));

    expect(await screen.findByText("1 visible · 2 loaded")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.queryByText("Row one")).not.toBeInTheDocument();
  });

  it("renders scheduled-run state, trigger, and filter labels in Chinese", async () => {
    const user = userEvent.setup();
    await testI18n.changeLanguage("zh");
    const scheduledTask = makeTask({ status: "enabled" });
    scheduledTask.task_type = "scheduled_run";
    const execution: ScheduledRunExecution = {
      id: "11111111-1111-1111-1111-111111111111",
      schedule_id: "22222222-2222-2222-2222-222222222222",
      workflow_id: "wf_42",
      run_key: "manual-1",
      status: "succeeded",
      trigger_type: "manual",
      triggered_at: "2026-05-24T10:00:00Z",
      started_at: "2026-05-24T10:00:01Z",
      finished_at: "2026-05-24T10:01:00Z",
      input_snapshot: {},
      result: {},
      results_uri: null,
      error: null,
      run_state: {},
      notification_state: { status: "skipped" },
    };
    vi.mocked(getTask).mockResolvedValue(scheduledTask);
    vi.mocked(getScheduledRun).mockResolvedValue({
      task: scheduledTask,
      schedule: {
        id: execution.schedule_id,
        task_id: TASK_ID,
        workflow_id: "wf_42",
        name: "每日汇总",
        enabled: true,
        schedule_type: "cron",
        cron_expr: "0 9 * * *",
        interval_seconds: null,
        timezone: "Asia/Shanghai",
        input_preset: {},
        mount_enabled: false,
        notification_policy: {},
        concurrency_policy: "skip",
        failure_policy: "continue",
        catchup_policy: false,
        next_run_at: "2026-05-25T01:00:00Z",
        end_at: null,
        last_run_at: "2026-05-24T01:00:00Z",
        last_status: "succeeded",
        created_at: "2026-05-20T01:00:00Z",
        updated_at: "2026-05-24T01:00:00Z",
      },
    });
    vi.mocked(listScheduledRunExecutions).mockResolvedValue({
      items: [execution],
      total: 1,
      limit: 50,
      offset: 0,
    });

    renderAt(TASK_ID);

    expect(await screen.findByText(/手动触发/)).toBeInTheDocument();
    expect(screen.getAllByText("成功").length).toBeGreaterThan(0);
    expect(screen.getByText(/未发送/)).toBeInTheDocument();
    const scheduleConfiguration = screen.getByTestId("schedule-configuration-details");
    expect(scheduleConfiguration.querySelector("dl")).toHaveClass("max-w-3xl", "sm:grid-cols-2");
    expect(screen.getByText("每日汇总").closest("div")).not.toHaveClass(
      "sm:grid-cols-[8.5rem_minmax(0,1fr)]",
    );
    expect(screen.getByText("每日汇总")).not.toHaveClass("text-right");
    expect(screen.getByText("每天 09:00")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "执行日志" }));
    expect(screen.getByRole("combobox", { name: "严重程度" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "事件类型" })).toBeInTheDocument();
    expect(screen.queryByText("succeeded")).not.toBeInTheDocument();
    expect(screen.queryByText("manual")).not.toBeInTheDocument();
  });

  it("deduplicates the live tail against history and forwards sort and time boundaries", async () => {
    const user = userEvent.setup();
    vi.mocked(getTask).mockResolvedValue(makeTask());
    vi.mocked(getTaskEvents).mockResolvedValue({
      items: [
        {
          id: 7,
          task_id: TASK_ID,
          ts: "2026-05-22T08:00:00Z",
          event_type: "log",
          payload: { level: "info", message: "historical copy" },
        },
      ],
      limit: 50,
      after_seq: null,
      before_seq: null,
      order: "desc",
      next_cursor: null,
      latest_seq: 7,
    });
    vi.mocked(useTaskStream).mockReturnValue({
      events: [{
        id: 7,
        event_type: "log",
        payload: {
          level: "info",
          message: "live replacement",
          _event_ts: "2026-05-22T08:00:00Z",
        },
      }],
      done: false,
    });

    renderAt(TASK_ID);
    await user.click(await screen.findByRole("tab", { name: "Execution logs" }));
    expect(await screen.findByText("live replacement")).toBeInTheDocument();
    expect(screen.queryByText("historical copy")).not.toBeInTheDocument();
    expect(screen.getByText("1 visible · 1 loaded")).toBeInTheDocument();

    await user.click(screen.getByRole("combobox", { name: "Sort" }));
    await user.click(screen.getByRole("option", { name: "Oldest first" }));
    await waitFor(() => {
      expect(getTaskEvents).toHaveBeenCalledWith(TASK_ID, expect.objectContaining({
        order: "asc",
      }));
    });

    await user.click(screen.getByRole("combobox", { name: "Time range" }));
    await user.click(screen.getByRole("option", { name: "Custom range" }));
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-05-20T08:00" } });
    fireEvent.change(screen.getByLabelText("To"), { target: { value: "2026-05-25T18:00" } });
    await waitFor(() => {
      expect(getTaskEvents).toHaveBeenCalledWith(TASK_ID, expect.objectContaining({
        from: new Date("2026-05-20T08:00").toISOString(),
        to: new Date("2026-05-25T18:00").toISOString(),
        order: "asc",
      }));
    });
  });

  it("loads the next event cursor without duplicating an overlapping event", async () => {
    const user = userEvent.setup();
    vi.mocked(getTask).mockResolvedValue(makeTask());
    vi.mocked(getTaskEvents)
      .mockResolvedValueOnce({
        items: [
          {
            id: 9,
            task_id: TASK_ID,
            ts: "2026-05-24T10:00:00Z",
            event_type: "log",
            payload: { level: "info", message: "latest event" },
          },
        ],
        limit: 50,
        after_seq: null,
        before_seq: null,
        order: "desc",
        next_cursor: 8,
        latest_seq: 9,
      })
      .mockResolvedValueOnce({
        items: [
          {
            id: 9,
            task_id: TASK_ID,
            ts: "2026-05-24T10:00:00Z",
            event_type: "log",
            payload: { level: "info", message: "overlapping copy" },
          },
          {
            id: 8,
            task_id: TASK_ID,
            ts: "2026-05-24T09:59:00Z",
            event_type: "log",
            payload: { level: "warning", message: "older event" },
          },
        ],
        limit: 50,
        after_seq: null,
        before_seq: null,
        order: "desc",
        next_cursor: null,
        latest_seq: 9,
      });

    renderAt(TASK_ID);
    await user.click(await screen.findByRole("tab", { name: "Execution logs" }));
    expect(await screen.findByText("latest event")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load older records" }));

    expect(await screen.findByText("older event")).toBeInTheDocument();
    expect(screen.getByText("2 visible · 2 loaded")).toBeInTheDocument();
    expect(screen.queryByText("overlapping copy")).not.toBeInTheDocument();
    expect(getTaskEvents).toHaveBeenLastCalledWith(TASK_ID, expect.objectContaining({
      before_seq: 8,
      limit: 50,
      order: "desc",
    }));
  });

  it("renders a recoverable event-history error and retries the query", async () => {
    const user = userEvent.setup();
    vi.mocked(getTask).mockResolvedValue(makeTask());
    vi.mocked(getTaskEvents)
      .mockRejectedValueOnce(new Error("events unavailable"))
      .mockResolvedValueOnce({
        items: [],
        limit: 50,
        after_seq: null,
        before_seq: null,
        order: "desc",
        next_cursor: null,
        latest_seq: 0,
      });

    renderAt(TASK_ID);
    await user.click(await screen.findByRole("tab", { name: "Execution logs" }));
    expect(await screen.findByText("Failed to load logs.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No events yet.")).toBeInTheDocument();
    expect(getTaskEvents).toHaveBeenCalledTimes(2);
  });
});
