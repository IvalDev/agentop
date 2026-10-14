import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import { listMembers, type TeamMember } from '../api/team';

interface TeamState {
  /** `null` until the first successful load. */
  members: TeamMember[] | null;
  loading: boolean;
  failed: boolean;
}

/** The company's people, loaded on mount; `upsert` applies what a change returned. */
export function useTeamMembers(api: ApiClient) {
  const [state, setState] = useState<TeamState>({ members: null, loading: true, failed: false });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    listMembers(api).then(
      (members) => {
        if (active) {
          setState({ members, loading: false, failed: false });
        }
      },
      () => {
        if (active) {
          setState((current) => ({ members: current.members, loading: false, failed: true }));
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, attempt]);

  const reload = useCallback(() => {
    setState((current) => ({ ...current, loading: true }));
    setAttempt((current) => current + 1);
  }, []);

  /** Replace the member with the same id, or add a new one at the end (oldest first). */
  const upsert = useCallback((member: TeamMember) => {
    setState((current) => {
      const members = current.members ?? [];
      const known = members.some((item) => item.id === member.id);
      return {
        ...current,
        members: known ? members.map((item) => (item.id === member.id ? member : item)) : [...members, member],
      };
    });
  }, []);

  return { ...state, reload, upsert };
}
