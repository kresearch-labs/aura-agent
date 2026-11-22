// Merge a fetched task with what the person is actually editing.
// Never blind-reset a form on a streaming refresh, and never let that refresh
// move an optimistic-save version forward behind an unfinished draft.
export type LiveTask = {
  id: number;
  version: number;
  title?: string;
  description?: string;
  assignees?: { name: string }[];
  labels?: string[];
  priority?: number;
  start?: number;
  due?: number;
  links?: { kind: string; other: number }[];
};

export type TaskDraft = {
  title: string;
  description: string;
  assignees: string;
  labels: string;
  priority: string;
  start: string;
  due: string;
};

export type EditedTaskPatch = {
  title?: string;
  description?: string;
  assignees?: string[];
  labels?: string[];
  priority?: number;
  start?: number | null;
  due?: number | null;
};

// Only the edited fields are replayed after a conflict. Compare the values
// that would actually reach the API, so list/number formatting cannot turn
// a request into an accidental overwrite of an unseen peer's field change.
export function editedTaskFields(previous: LiveTask, current: TaskDraft): EditedTaskPatch {
  const before = formFromTask(previous);
  const list = (text: string) =>
    text.split(",").map((value) => value.trim()).filter(Boolean);
  const equalLists = (a: string[], b: string[]) =>
    a.length === b.length && a.every((value, i) => value === b[i]);
  const date = (value: string): number | null =>
    value ? Date.parse(value) / 1000 : null;
  const patch: EditedTaskPatch = {};
  const nextTitle = current.title.trim();
  // Compare outbounds to what is really stored, but only after an explicit
  // edit. Saving another field must not normalize an untouched padded title.
  if (current.title !== before.title && nextTitle !== before.title)
    patch.title = nextTitle;
  if (current.description !== before.description)
    patch.description = current.description;
  for (const field of ["assignees", "labels"] as const) {
    const next = list(current[field]);
    if (!equalLists(list(before[field]), next)) {
      // The object key is identical to the Draft and API field here.
      (patch as Record<string, unknown>)[field] = next;
    }
  }
  const priority = Number.parseInt(current.priority, 10) || 0;
  if (priority !== (previous.priority ?? 0)) patch.priority = priority;
  if (current.start !== before.start) patch.start = date(current.start);
  if (current.due !== before.due) patch.due = date(current.due);
  return patch;
}

export type DraftMerge = {
  form: TaskDraft;
  baseVersion: number;
  dirty: boolean;
  remoteChange: boolean;
  outdated: boolean;
};

const FIELDS = ["title", "description", "assignees", "labels",
                "priority", "start", "due"] as const;

export function isCurrentCommentRead(
  requestedTaskId: number | undefined,
  openTaskId: number | undefined,
  requestEpoch: number,
  latestEpoch: number,
): boolean {
  return requestedTaskId !== undefined && requestedTaskId === openTaskId &&
         requestEpoch === latestEpoch;
}

export function formFromTask(task: LiveTask | null): TaskDraft {
  return {
    title: task?.title ?? "",
    description: task?.description ?? "",
    assignees: (task?.assignees ?? []).map((a) => a.name).join(", "),
    labels: (task?.labels ?? []).join(", "),
    priority: String(task?.priority ?? 0),
    // Keep the existing editor's date conversion and empty-value semantics.
    start: task?.start ? new Date(task.start * 1000).toISOString().slice(0, 10) : "",
    due: task?.due ? new Date(task.due * 1000).toISOString().slice(0, 10) : "",
  };
}

export function snapshotLiveTask<T extends LiveTask>(
  task: T | null, baseVersion?: number,
): T | null {
  if (!task) return null;
  // Only the editor's remembered draft baseline uses the older save anchor.
  // props.task and its authoritative version are never modified.
  return {...task, version: baseVersion ?? task.version};
}

export function mergeTaskDraft(
  previous: LiveTask | null,
  current: TaskDraft,
  incoming: LiveTask | null,
  explicitReset = false,
): DraftMerge {
  const fresh = formFromTask(incoming);
  if (explicitReset || !previous || !incoming || previous.id !== incoming.id) {
    return {
      form: fresh, baseVersion: incoming?.version ?? 0,
      dirty: false, remoteChange: false, outdated: false,
    };
  }

  const before = formFromTask(previous);
  const dirty = FIELDS.some((field) => current[field] !== before[field]);
  const merge: TaskDraft = {...fresh};
  for (const field of FIELDS) {
    if (current[field] !== before[field]) merge[field] = current[field];
  }

  // Transport epochs prevent most late responses. This is also the final
  // defensive gate for equi-id out-of-order REST answers.
  if (incoming.version < previous.version) {
    return {
      form: {...current}, baseVersion: previous.version,
      dirty, remoteChange: false, outdated: true,
    };
  }

  return {
    form: merge,
    // A live read is NOT permission to overwrite someone else's next-version
    // writes. Only a clean draft may advance the version used by PATCH.
    baseVersion: dirty ? previous.version : incoming.version,
    dirty,
    remoteChange: dirty && incoming.version > previous.version,
    outdated: false,
  };
}
