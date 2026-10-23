import { DurableObject } from "cloudflare:workers";

interface Env {
  REVIEW_APP: DurableObjectNamespace;
  DSH_LLM_API_KEY: string;
  DSH_LLM_BASE_URL?: string;
  DSH_LLM_MODEL?: string;
}

/**
 * 一个 Durable Object 对应一个容器实例。
 * 为什么只开 1 个实例：会话（上传后的文本）存在容器内存里，
 * 多实例会出现「A 实例建的会话，B 实例读不到」。
 * 要扩容得先把会话挪到 KV/R2，见 docs/部署-Cloudflare方案.md。
 */
export class ReviewApp extends DurableObject<Env> {
  private starting?: Promise<void>;

  async fetch(request: Request): Promise<Response> {
    // 并发请求共享同一次启动与就绪检查
    this.starting ??= this.startAndWait().finally(() => {
      this.starting = undefined;
    });
    await this.starting;

    const url = new URL(request.url);
    url.protocol = "http:";
    url.host = "container";
    const forwarded = new Request(url, request);
    forwarded.headers.delete("host");
    return this.ctx.container.getTcpPort(8080).fetch(forwarded);
  }

  private async startAndWait(): Promise<void> {
    const container = this.ctx.container;

    if (!container.running) {
      container.start({
        image: container.images.base,
        instance: "basic",            // lite=256MiB 太小，PyMuPDF 处理大 PDF 会 OOM
        enableInternet: true,         // 容器要能访问 api.deepseek.com
        env: {
          HOST: "0.0.0.0",
          PORT: "8080",
          REPORTS_DIR: "/tmp/reports", // 容器磁盘是临时的：公开站点不该长期留别人简历
          RATE_PER_HOUR: "20",
          DAILY_LIMIT: "300",
          DSH_LLM_THINKING: "off",
          DSH_LLM_TIMEOUT: "300",
          // 下面三个来自 Worker secret / vars，只在启动时读一次
          DSH_LLM_API_KEY: this.env.DSH_LLM_API_KEY,
          DSH_LLM_BASE_URL: this.env.DSH_LLM_BASE_URL ?? "https://api.deepseek.com/v1",
          DSH_LLM_MODEL: this.env.DSH_LLM_MODEL ?? "deepseek-flash",
        },
      });
    }

    // 10 分钟没请求就休眠，省钱
    await container.setInactivityTimeout(10 * 60 * 1000);

    const port = container.getTcpPort(8080);
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 100; attempt++) {
      try {
        const res = await port.fetch("http://container/api/health", {
          signal: AbortSignal.timeout(1000),
        });
        await res.body?.cancel();
        if (res.ok) return;
        lastError = new Error(`health ${res.status}`);
      } catch (err) {
        lastError = err;
      }
      await scheduler.wait(300);
    }
    throw new Error("容器没有在 8080 端口就绪", { cause: lastError });
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/__ping") return new Response("ok");
    return env.REVIEW_APP.getByName("main").fetch(request);
  },
};
