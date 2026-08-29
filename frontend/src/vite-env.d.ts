/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_PROXY_TARGET?: string;
  readonly VITE_TRADING_MODE?: string;
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_DAY_LOSS_LIMIT?: string;
  readonly VITE_STALE_MINUTES?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
