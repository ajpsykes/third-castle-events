import test from "node:test";
import assert from "node:assert/strict";

import { keyFromPathname } from "../src/index.mjs";

test("accepts only immutable derived WebPs and the current manifest", () => {
  assert.equal(
    keyFromPathname("/events/third-castle/0123456789abcdefabcd.webp"),
    "events/third-castle/0123456789abcdefabcd.webp",
  );
  assert.equal(
    keyFromPathname("/manifests/third-castle/current.json"),
    "manifests/third-castle/current.json",
  );
});

test("rejects arbitrary paths before they can touch R2", () => {
  assert.equal(keyFromPathname("/"), null);
  assert.equal(keyFromPathname("/events/third-castle/not-a-hash.webp"), null);
  assert.equal(keyFromPathname("/events/third-castle/0123456789abcdefabcd.jpg"), null);
  assert.equal(keyFromPathname("/manifests/other.json"), null);
  assert.equal(keyFromPathname("/%zz"), null);
});
