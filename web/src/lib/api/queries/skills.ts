import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import {
  deleteSkill,
  getSkillDraft,
  getSkill,
  getSkillVersion,
  installSkillCatalogItem,
  listSkills,
  listSkillVersions,
  publishSkillVersion,
  resolveSkillCatalogItem,
  saveCustomSkill,
  saveSkillDraft,
  searchSkillCatalog,
} from '@/lib/api/skills';
import type { SkillCatalogSource } from '@/lib/api/skills';

const LIST_KEY = ['skills', 'list'] as const;
const itemKey = (id: string) => ['skills', 'item', id] as const;
const draftKey = (id: string) => ['skills', 'draft', id] as const;
const versionsKey = (id: string) => ['skills', 'versions', id] as const;

export const useSkills = (opts?: { enabled?: boolean; refreshOnReturn?: boolean }) =>
  useQuery({
    queryKey: LIST_KEY,
    queryFn: listSkills,
    enabled: opts?.enabled ?? true,
    ...(opts?.refreshOnReturn ? {
      refetchOnWindowFocus: 'always' as const,
      refetchOnMount: 'always' as const,
    } : {}),
  });

export const useSkill = (id: string | undefined) =>
  useQuery({
    queryKey: itemKey(id ?? ''),
    queryFn: () => getSkill(id as string),
    enabled: !!id,
  });

export const useSkillDraft = (id: string | undefined, enabled = true) =>
  useQuery({
    queryKey: draftKey(id ?? ''),
    queryFn: () => getSkillDraft(id as string),
    enabled: !!id && enabled,
  });

export const useSkillVersions = (id: string | undefined) =>
  useQuery({
    queryKey: versionsKey(id ?? ''),
    queryFn: () => listSkillVersions(id as string),
    enabled: !!id,
  });

export const useSkillVersion = (
  id: string | undefined,
  revisionId: string | undefined,
) =>
  useQuery({
    queryKey: [...versionsKey(id ?? ''), revisionId ?? ''],
    queryFn: () => getSkillVersion(id as string, revisionId as string),
    enabled: !!id && !!revisionId,
  });

export const useSkillCatalog = (
  source: SkillCatalogSource,
  search: string,
  limit: number,
  opts?: { enabled?: boolean },
) =>
  useQuery({
    queryKey: ['skills', 'catalog', source, search.trim(), limit],
    queryFn: () => searchSkillCatalog(source, search.trim(), limit),
    enabled: opts?.enabled ?? true,
    placeholderData: keepPreviousData,
    staleTime: 5 * 60 * 1000,
  });

export const useSkillCatalogItem = (
  source: SkillCatalogSource | undefined,
  sourceId: string | undefined,
) =>
  useQuery({
    queryKey: ['skills', 'catalog-item', source ?? '', sourceId ?? ''],
    queryFn: () => resolveSkillCatalogItem(source as SkillCatalogSource, sourceId as string),
    enabled: !!source && !!sourceId,
    staleTime: 5 * 60 * 1000,
  });

export const useInstallSkillCatalogItem = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ source, sourceId }: { source: SkillCatalogSource; sourceId: string }) =>
      installSkillCatalogItem(source, sourceId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LIST_KEY }),
  });
};

export const useDeleteSkill = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: deleteSkill,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: LIST_KEY }),
  });
};

export const useSaveCustomSkill = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: saveCustomSkill,
    onSuccess: (skill) => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY });
      // The upload response is the list-card shape, not SkillDetail. Seeding
      // the detail cache with it leaves files/skill_md/body undefined and
      // crashes the detail page before a GET can run.
      void queryClient.invalidateQueries({ queryKey: itemKey(skill.id) });
    },
  });
};

export const useSaveSkillDraft = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, skillMd }: { id: string; skillMd: string }) =>
      saveSkillDraft(id, skillMd),
    onSuccess: (draft) => {
      queryClient.setQueryData(draftKey(draft.skill_id), draft);
      void queryClient.invalidateQueries({ queryKey: itemKey(draft.skill_id) });
    },
  });
};

export const usePublishSkillVersion = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      publishSkillVersion(id, version),
    onSuccess: (skill) => {
      void queryClient.invalidateQueries({ queryKey: LIST_KEY });
      void queryClient.invalidateQueries({ queryKey: itemKey(skill.id) });
      void queryClient.invalidateQueries({ queryKey: draftKey(skill.id) });
      void queryClient.invalidateQueries({ queryKey: versionsKey(skill.id) });
    },
  });
};
