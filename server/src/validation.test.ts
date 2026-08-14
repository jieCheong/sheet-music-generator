import assert from "node:assert/strict";
import { test } from "node:test";
import { validateYoutubeUrl } from "./validation.js";

test("accepts a standard watch URL", () => {
  assert.equal(validateYoutubeUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ"), true);
});

test("accepts bare youtube.com and m.youtube.com hosts", () => {
  assert.equal(validateYoutubeUrl("https://youtube.com/watch?v=abc123"), true);
  assert.equal(validateYoutubeUrl("https://m.youtube.com/watch?v=abc123"), true);
});

test("accepts a youtu.be short URL", () => {
  assert.equal(validateYoutubeUrl("https://youtu.be/dQw4w9WgXcQ"), true);
});

test("rejects a non-YouTube host", () => {
  assert.equal(validateYoutubeUrl("https://vimeo.com/12345"), false);
});

test("rejects a watch URL with no video id", () => {
  assert.equal(validateYoutubeUrl("https://www.youtube.com/watch"), false);
  assert.equal(validateYoutubeUrl("https://www.youtube.com/watch?v="), false);
});

test("rejects a youtu.be URL with no path", () => {
  assert.equal(validateYoutubeUrl("https://youtu.be/"), false);
});

test("rejects a malformed URL", () => {
  assert.equal(validateYoutubeUrl("not a url"), false);
});

test("rejects a non-http(s) protocol", () => {
  assert.equal(validateYoutubeUrl("ftp://youtube.com/watch?v=abc123"), false);
});
