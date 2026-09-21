'use client';
import { useSyncExternalStore } from 'react';
import { getLocale, getServerLocale, subscribeLocale } from './core';
export function useLocale() { return useSyncExternalStore(subscribeLocale, getLocale, getServerLocale); }
