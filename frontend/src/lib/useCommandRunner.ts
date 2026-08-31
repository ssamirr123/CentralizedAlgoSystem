import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAlgoActionMutation } from "@/api/hooks";
import { getCommand } from "@/api/endpoints";
import type { AlgoAction } from "@/api/types";

export interface CommandRun {
  key: string;
  at: string;
  action: AlgoAction;
  algoId: string;
  serverId: string;
  commandId: number | null;
  jobId: string | null;
  status: string;
  message: string | null;
}

const TERMINAL = new Set(["RUNNING", "STOPPED", "ERROR", "FAILED", "SUCCESS", "UNKNOWN", "UPDATED"]);

/**
 * Issues an algo process-control command (POST /api/algo/{action}) and
 * polls GET /api/command/{id} until it reaches a final state — the same
 * "never claim RUNNING just because the API accepted it" contract the
 * Commands screen uses. Shared so the Algorithms page doesn't duplicate it.
 */
export function useCommandRunner(max = 30) {
  const mutation = useAlgoActionMutation();
  const qc = useQueryClient();
  const [runs, setRuns] = useState<CommandRun[]>([]);
  const timers = useRef<Set<number>>(new Set());

  useEffect(
    () => () => {
      timers.current.forEach((t) => window.clearTimeout(t));
      timers.current.clear();
    },
    [],
  );

  const patch = useCallback((commandId: number, p: Partial<CommandRun>) => {
    setRuns((prev) => prev.map((r) => (r.commandId === commandId ? { ...r, ...p } : r)));
  }, []);

  const poll = useCallback(
    (commandId: number) => {
      let tries = 0;
      const tick = async () => {
        tries += 1;
        try {
          const res = await getCommand(commandId);
          patch(commandId, { status: res.status, message: res.message ?? null });
          if (!TERMINAL.has(res.status.toUpperCase()) && tries < 40) {
            const t = window.setTimeout(tick, 3000);
            timers.current.add(t);
          } else {
            qc.invalidateQueries({ queryKey: ["algos"] });
            qc.invalidateQueries({ queryKey: ["algo-status"] });
            qc.invalidateQueries({ queryKey: ["pnl-today"] });
          }
        } catch (e) {
          patch(commandId, { message: e instanceof Error ? e.message : "poll failed" });
        }
      };
      const t = window.setTimeout(tick, 2000);
      timers.current.add(t);
    },
    [patch, qc],
  );

  const run = useCallback(
    async (action: AlgoAction, algoId: string, serverId: string) => {
      const base = {
        key: `${algoId}|${serverId}|${Date.now()}`,
        at: new Date().toISOString(),
        action,
        algoId,
        serverId,
      };
      try {
        const res = await mutation.mutateAsync({ action, algoId, serverId, requestedBy: "control-center-ui" });
        setRuns((prev) =>
          [
            {
              ...base,
              commandId: res.command_id,
              jobId: res.job_id,
              status: res.status,
              message: res.message ?? null,
            },
            ...prev,
          ].slice(0, max),
        );
        if (res.command_id != null) poll(res.command_id);
        return res;
      } catch (e) {
        setRuns((prev) =>
          [
            {
              ...base,
              commandId: null,
              jobId: null,
              status: "FAILED",
              message: e instanceof Error ? e.message : "request failed",
            },
            ...prev,
          ].slice(0, max),
        );
        throw e;
      }
    },
    [mutation, poll, max],
  );

  return { run, runs, isPending: mutation.isPending };
}
