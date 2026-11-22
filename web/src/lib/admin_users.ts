import type { Project } from "../team/frame/shell_state";

export type AdminPerson = {
  id: number; team_role?: string | null; role?: string | null;
  global_role?: string | null;
};
export type ProjectRole = {
  projectId: number; projectName: string; role: string;
  source: "membership" | "team";
  archived: boolean;
};

export function isFixedOwner(person: Pick<AdminPerson, "team_role" | "role">
                             | null | undefined): boolean {
  return person?.team_role === "owner" || person?.role === "owner";
}
export function isTeamAdministrator(person: Pick<AdminPerson, "team_role" | "role">
                                    | null | undefined): boolean {
  return isFixedOwner(person) || person?.team_role === "admin" || person?.role === "admin";
}
export function canAdministerSessions(person: AdminPerson,
                                      actor: AdminPerson | null | undefined): boolean {
  return isTeamAdministrator(actor) &&
    (!isFixedOwner(person) || isFixedOwner(actor));
}

export const GLOBAL_STAFF_MANAGER = "staff and projects manager";
export type AdminModule = "users" | "access" | "projects" | "roles" | "hub";

/**
 * The stand-in author of messages from a linked chat app — never a person.
 */
export function isLinkedApp(p: { name?: string | null; role?: string | null; team_role?: string | null }): boolean {
  return p?.name === "linked-app" || p?.role === "linked-app" || p?.team_role === "linked-app";
}
export function canOpenAdministration(person: AdminPerson | null | undefined): boolean {
  return isTeamAdministrator(person) || person?.global_role === GLOBAL_STAFF_MANAGER;
}

export function adminTabsFor(person: AdminPerson | null | undefined): AdminModule[] {
  // the two "one person" places together, then what changes for everyone
  if (isTeamAdministrator(person)) return ["users", "access", "projects", "roles", "hub"];
  return canOpenAdministration(person) ? ["users", "access", "projects"] : [];
}

export function projectPermissionGranted(
  permissions: ReadonlySet<string> | null | undefined, permission: string,
): boolean {
  return permissions?.has(permission) === true;
}

export function canManageAccount(person: AdminPerson,
                                 actor: AdminPerson | null | undefined): boolean {
  if (!canOpenAdministration(actor)) return false;
  if (isFixedOwner(person)) return actor?.id === person.id && isFixedOwner(actor);
  return isTeamAdministrator(actor) || !isTeamAdministrator(person);
}

// These are the legacy role strings still understood by perms.allowed().
export function projectRoleLabel(role: string | null | undefined): string {
  if (role === "manager") return "Project admin";
  if (role === "member") return "Member";
  if (role === "viewer") return "Reader";
  return role || "Unnamed role";
}

export function projectRolesFor(person: AdminPerson,
                                projects: readonly Project[]): ProjectRole[] {
  const teamRole = isFixedOwner(person) ? "owner" : isTeamAdministrator(person) ? "admin" : "user";
  return projects.flatMap((project): ProjectRole[] => {
    const member = (project.members ?? []).find(m => m.person === person.id);
    if (member) {
      return [{ projectId: project.id, projectName: project.name,
                role: projectRoleLabel(member.role), source: "membership",
                archived: Boolean(project.archived_at) } satisfies ProjectRole];
    }
    if (teamRole !== "owner" && teamRole !== "admin") return [];
    return [{ projectId: project.id, projectName: project.name,
              role: `${teamRole === "owner" ? "Owner" : "Admin"} · team access`,
              source: "team", archived: Boolean(project.archived_at) } satisfies ProjectRole];
  });
}
