const ALLOWED_HOSTS = new Set(["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"]);

export function validateYoutubeUrl(input: string): boolean {
  let url: URL;
  try {
    url = new URL(input);
  } catch {
    return false;
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") return false;

  const host = url.hostname.toLowerCase();
  if (!ALLOWED_HOSTS.has(host)) return false;

  if (host === "youtu.be") {
    return url.pathname.length > 1;
  }

  const videoId = url.searchParams.get("v");
  return videoId !== null && videoId.trim().length > 0;
}
