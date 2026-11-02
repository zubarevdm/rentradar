// Cloudflare Worker: прозрачный relay к api.telegram.org для серверов, где Telegram недоступен.
// Пропускает только пути /bot<токен>/... и /file/bot<токен>/... нашего бота (BOT_ID — число до «:» в токене).
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const allowed = new RegExp(`^/(file/)?bot${env.BOT_ID}:[\\w-]+/`);
    if (!allowed.test(url.pathname)) return new Response("not found", { status: 404 });
    const target = "https://api.telegram.org" + url.pathname + url.search;
    return fetch(new Request(target, request));
  },
};
