"use client";

import {
  Check,
  Copy,
  ExternalLink,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useState } from "react";

type LaunchStatus = "preparing" | "ready" | "opened" | "error";

type TelegramWebApp = {
  ready?: () => void;
  expand?: () => void;
  openLink?: (
    url: string,
    options?: { try_instant_view?: boolean },
  ) => void;
};

declare global {
  interface Window {
    Telegram?: {
      WebApp?: TelegramWebApp;
    };
  }
}

function loadTelegramSdk(): Promise<void> {
  if (window.Telegram?.WebApp) return Promise.resolve();
  const existing = document.querySelector<HTMLScriptElement>(
    'script[data-antra-telegram-sdk="true"]',
  );
  if (existing) {
    return new Promise((resolve) => {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => resolve(), { once: true });
      window.setTimeout(resolve, 1500);
    });
  }

  return new Promise((resolve) => {
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-web-app.js?59";
    script.async = true;
    script.dataset.antraTelegramSdk = "true";
    script.addEventListener("load", () => resolve(), { once: true });
    script.addEventListener("error", () => resolve(), { once: true });
    document.head.appendChild(script);
    window.setTimeout(resolve, 1500);
  });
}

function validatedExternalUrl(value: unknown): string {
  if (typeof value !== "string" || !value) return "";
  try {
    const target = new URL(value);
    if (
      target.protocol !== "https:" ||
      target.origin !== window.location.origin ||
      target.pathname !== "/" ||
      target.search
    ) {
      return "";
    }
    const credentials = new URLSearchParams(target.hash.replace(/^#/, ""));
    if (!credentials.get("token") || credentials.get("api") !== target.origin) {
      return "";
    }
    return target.toString();
  } catch {
    return "";
  }
}

async function copyText(value: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    return copied;
  }
}

export function ExternalBrowserLauncher() {
  const [status, setStatus] = useState<LaunchStatus>("preparing");
  const [externalUrl, setExternalUrl] = useState("");
  const [copied, setCopied] = useState(false);
  const [sdkResolved, setSdkResolved] = useState(false);
  const [bridgeReady, setBridgeReady] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const launch = new URLSearchParams(window.location.search).get("launch") ?? "";

    void loadTelegramSdk().then(() => {
      const webApp = window.Telegram?.WebApp;
      webApp?.ready?.();
      webApp?.expand?.();
      setBridgeReady(typeof webApp?.openLink === "function");
      setSdkResolved(true);
    });

    if (!launch || launch.length > 128) {
      const showError = window.setTimeout(() => setStatus("error"), 0);
      return () => {
        window.clearTimeout(showError);
        controller.abort();
      };
    }

    void fetch("/api/v1/player-launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ launch }),
      signal: controller.signal,
      cache: "no-store",
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(`launch exchange failed: ${response.status}`);
        return response.json() as Promise<{ url?: unknown }>;
      })
      .then((payload) => {
        const url = validatedExternalUrl(payload.url);
        if (!url) throw new Error("invalid external player URL");
        setExternalUrl(url);
        setStatus("ready");
        window.history.replaceState(null, document.title, "/open");
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setStatus("error");
      });

    return () => controller.abort();
  }, []);

  const openExternally = () => {
    if (!externalUrl) return;
    const telegram = window.Telegram?.WebApp;
    if (telegram?.openLink) {
      telegram.openLink(externalUrl, { try_instant_view: false });
      setStatus("opened");
    }
  };

  const copyExternalUrl = async () => {
    if (!externalUrl) return;
    const didCopy = await copyText(externalUrl);
    setCopied(didCopy);
    if (didCopy) window.setTimeout(() => setCopied(false), 2500);
  };

  return (
    <main className="centered-shell external-launcher">
      <div className="brand" aria-label="Antra Player">
        <span className="brand-mark" aria-hidden="true">A</span>
        <span>
          <strong>ANTRA</strong>
          <small>PLAYER</small>
        </span>
      </div>

      <section className="external-launch-card" aria-busy={status === "preparing"}>
        <div className="empty-icon" aria-hidden="true">
          {status === "preparing" ? (
            <LoaderCircle className="spin" size={28} />
          ) : (
            <ShieldCheck size={28} />
          )}
        </div>

        {status === "error" ? (
          <>
            <h1>Ссылка уже использована</h1>
            <p>Вернитесь в бот и нажмите команду /player ещё раз.</p>
            <a className="primary-action" href="https://t.me/fnnlinkbot">
              Вернуться в Telegram
            </a>
          </>
        ) : (
          <>
            <h1>Открыть внешний браузер</h1>
            <p>
              Сам плеер не запускается внутри Telegram. Нажмите кнопку ниже,
              чтобы открыть его в Safari, Chrome или браузере по умолчанию.
            </p>
            <div className="launcher-actions">
              <button
                className="primary-action"
                type="button"
                onClick={openExternally}
                disabled={!externalUrl || !bridgeReady}
              >
                <ExternalLink size={18} aria-hidden="true" />
                {status === "preparing"
                  ? "Готовим безопасную ссылку…"
                  : !bridgeReady
                    ? "Подключаем внешний браузер…"
                  : status === "opened"
                    ? "Открыть ещё раз"
                    : "Открыть во внешнем браузере"}
              </button>
              <button
                className="secondary-action"
                type="button"
                onClick={() => void copyExternalUrl()}
                disabled={!externalUrl}
              >
                {copied ? (
                  <Check size={18} aria-hidden="true" />
                ) : (
                  <Copy size={18} aria-hidden="true" />
                )}
                {copied ? "Ссылка скопирована" : "Скопировать ссылку"}
              </button>
            </div>
            {status === "opened" && (
              <p className="launcher-note">
                Если браузер не открылся, скопируйте ссылку и вставьте её в Safari или Chrome.
              </p>
            )}
            {status !== "opened" && sdkResolved && !bridgeReady && (
              <p className="launcher-note">
                Эта версия Telegram не предоставила внешний переход. Скопируйте ссылку
                и вставьте её в Safari или Chrome.
              </p>
            )}
          </>
        )}
      </section>
    </main>
  );
}
