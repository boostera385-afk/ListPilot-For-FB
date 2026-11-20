// Local schema regression tests; never contacts Google or changes a real sheet.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const lock = {
  held: false,
  waitLock() {
    assert.equal(this.held, false);
    this.held = true;
  },
  releaseLock() { this.held = false; },
};
const context = vm.createContext({LockService: {getScriptLock: () => lock}});
vm.runInContext(fs.readFileSync(path.join(__dirname, "../core/assets/apps_script.js"), "utf8"), context);

class FakeSheet {
  constructor(rows, name = "Live Checker") {
    this.rows = rows.map(row => [...row]);
    this.name = name;
    this.insertions = [];
  }
  getName() { return this.name; }
  getLastColumn() { return this.rows[0].length; }
  insertColumnAfter(position) {
    this.insertions.push(position);
    this.rows.forEach(row => row.splice(position, 0, ""));
    return this;
  }
  getRange(row, column, height = 1, width = 1) {
    const sheet = this;
    return {
      getValues() {
        return Array.from({length: height}, (_, r) =>
          Array.from({length: width}, (_, c) => sheet.rows[row - 1 + r]?.[column - 1 + c] ?? ""));
      },
      setValue(value) {
        sheet.rows[row - 1] ??= [];
        sheet.rows[row - 1][column - 1] = value;
        return this;
      },
      setFontWeight() { return this; },
      copyFormatToRange() { return this; },
    };
  }
}

const existing = new FakeSheet([
  ["ID", "PK Check", "US Check", "Visibility", "Custom formula"],
  ["seller", "2/3 in search", "3/3 in search", "PARTIAL", "=1+1"],
]);
assert.equal(context.ensureColumn_(existing, "PK vs US"), 4);
assert.deepEqual(existing.rows[0], ["ID", "PK Check", "US Check", "PK vs US", "Visibility", "Custom formula"]);
assert.deepEqual(existing.rows[1], ["seller", "2/3 in search", "3/3 in search", "", "PARTIAL", "=1+1"]);
assert.equal(context.ensureColumn_(existing, "PK vs US"), 4);
assert.deepEqual(existing.insertions, [3], "repeat setup must not insert a second comparison column");
assert.equal(lock.held, false);

const fresh = new FakeSheet([["ID", "Profile Name", "Status"]]);
context.ensureAutoColumns_(fresh);
const headers = [...fresh.rows[0]];
const us = headers.indexOf("US Check");
assert.ok(us > 0);
assert.equal(headers[us + 1], "PK vs US");
assert.equal(headers[us + 2], "Visibility");
assert.ok(headers.includes("Buyer Listing Targets"), "buyer-only mode needs explicit target input");
assert.deepEqual(headers.slice(0, 5), ["ID", "Profile Name", "Buyer Summary", "Next Step", "Buyer Search Area"]);
context.ensureAutoColumns_(fresh);
assert.deepEqual(fresh.rows[0], headers, "setup is idempotent");

const reordered = new FakeSheet([["ID", "PK vs US", "PK Check", "US Check", "Visibility"]]);
assert.equal(context.ensureColumn_(reordered, "PK vs US"), 2);
assert.equal(reordered.insertions.length, 0, "never duplicate a user-reordered existing column");

const other = new FakeSheet([["ID", "US Check", "Notes"]], "FB Listings");
assert.equal(context.ensureColumn_(other, "PK vs US"), 4);
assert.equal(other.insertions.length, 0, "special insertion belongs only to Live Checker");
const withData = new FakeSheet([
  ["ID", "Profile Name", "Status", "Custom formula"],
  ["seller-id", "Seller", "Success", "=1+1"],
]);
context.ensureAutoColumns_(withData);
assert.equal(withData.rows[1][withData.rows[0].indexOf("Custom formula")], "=1+1");
assert.equal(withData.rows[1][withData.rows[0].indexOf("Status")], "Success");
assert.deepEqual(withData.rows[1].slice(0, 5), ["seller-id", "Seller", "", "", ""]);
console.log("Apps Script region comparison schema tests passed.");
