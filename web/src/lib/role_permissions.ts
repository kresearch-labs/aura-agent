export type Role = { id: number; name: string; builtin?: boolean;
  permissions?: Record<string, string[] | Record<string, boolean>> };

// The frontend's vocabulary matches teamserver/perms.py::MODULE_OF.
// Permission enforcement remains on the server, never in these checkboxes.
export const PERMISSION_GROUPS: {
  module: string; title: string; ticks: { key: string; label: string }[];
}[] = [
  {module: "board", title: "Tasks", ticks: [
    {key: "view_work_packages", label: "View tasks"},
    {key: "add_work_packages", label: "Add tasks"},
    {key: "edit_work_packages", label: "Edit tasks"},
    {key: "move_work_packages", label: "Move tasks"},
    {key: "add_work_package_comments", label: "Comment on tasks"},
    {key: "manage_work_package_relations", label: "Manage task links"},
    {key: "add_work_package_attachments", label: "Add attachments"},
    {key: "export_work_packages", label: "Export tasks"},
    {key: "delete_work_packages", label: "Delete tasks"},
  ]},
  {module: "tree", title: "Files", ticks: [
    {key: "browse_repository", label: "Browse files"},
  ]},
  {module: "flow", title: "Flow", ticks: [
    {key: "manage_gantt", label: "Manage the timeline"},
  ]},
  {module: "history", title: "History and Now", ticks: [
    {key: "view_activity", label: "View activity"},
    {key: "export_activity", label: "Export history"},
    {key: "manage_activity", label: "Manage history"},
  ]},
  {module: "chat", title: "Chat", ticks: [
    {key: "view_messages", label: "Read messages"},
    {key: "add_messages", label: "Post messages"},
    {key: "edit_own_messages", label: "Edit own messages"},
    {key: "edit_messages", label: "Edit others' messages"},
    {key: "delete_messages", label: "Delete messages"},
  ]},
  {module: "aura_work", title: "Aura work", ticks: [
    {key: "aura_work", label: "Use Aura work"},
  ]},
  {module: "members", title: "Members", ticks: [
    {key: "view_members", label: "View members"},
    {key: "manage_members", label: "Manage members"},
  ]},
  {module: "settings", title: "Project settings", ticks: [
    {key: "edit_project", label: "Edit project"},
    {key: "archive_project", label: "Archive project (needs Edit project too)"},
  ]},
];
