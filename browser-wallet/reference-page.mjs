// What a person does in the reference web wallet, for the tests in this folder.

import { Browser, PRESS, sleep } from "./browser.mjs";
import { PASSWORD, trai } from "./wallet-page.mjs";

/** What a person does in the page, by what the page says: it has no ids to go by. */
export class ReferencePage extends Browser {
  /** A button by its words, in the open dialog if there is one; the last of them, where a form's submit is. */
  button(words) {
    return `(() => { const scope = [...document.querySelectorAll('dialog[open]')].pop() ?? document; return [...scope.querySelectorAll('button')].filter((button) => button.textContent.trim().startsWith(${JSON.stringify(words)}) && button.getClientRects().length > 0).pop() ?? null; })()`;
  }

  async press(words, seconds = 60) {
    await this.until(`(() => { const button = ${this.button(words)}; return Boolean(button) && !button.disabled; })()`, seconds, `the button ${words}`);
    await this.eval(`(() => { const button = ${this.button(words)}; ${PRESS("button")} button.click(); })()`);
  }

  /** An input by the words of its label: set the way React hears it. */
  async type(label, value) {
    const found = `(() => { const scope = [...document.querySelectorAll('dialog[open]')].pop() ?? document; const field = [...scope.querySelectorAll('label')].find((node) => node.textContent.includes(${JSON.stringify(label)})); return field?.querySelector('input, textarea') ?? null; })()`;
    await this.until(`Boolean(${found})`, 30, `the field ${label}`);
    await this.eval(`(() => { const input = ${found}; ${PRESS("input")} const prototype = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(prototype, 'value').set.call(input, ${JSON.stringify(value)}); input.dispatchEvent(new Event('input', { bubbles: true })); })()`);
  }

  says(words) {
    return `[...document.querySelectorAll('body *')].some((node) => node.children.length === 0 && node.textContent.includes(${JSON.stringify(words)}))`;
  }

  async restore(phrase) {
    await this.press("Restore", 30);
    await this.type("Recovery phrase", phrase);
    await this.type("New password", PASSWORD);
    await this.type("Repeat the password", PASSWORD);
    await this.press("Restore", 30);
    await this.until(`Boolean(document.querySelector('.tabs'))`, 60, "the wallet to open");
  }

  async unlock() {
    await this.type("Password", PASSWORD);
    await this.press("Unlock", 30);
    await this.until(`Boolean(document.querySelector('.tabs'))`, 60, "the wallet to open");
  }

  /** A wallet that locked itself after its idle time opens again, as a person coming back unlocks it. */
  async awake() {
    if (await this.eval(`!document.querySelector('.tabs') && ${this.button("Unlock")} !== null`)) await this.unlock();
  }

  async tab(words) {
    await this.until(`[...document.querySelectorAll('.tabs button')].some((button) => button.textContent.includes(${JSON.stringify(words)}))`, 60, `the tab ${words}`);
    await this.eval(`[...document.querySelectorAll('.tabs button')].find((button) => button.textContent.includes(${JSON.stringify(words)})).click()`);
  }

  /** The page in the sizes and designs people use, for a look by eye. */
  async gallery(prefix) {
    const design = (light) => this.eval(`(() => {
      if ((document.documentElement.dataset.theme === "light") !== ${light}) document.querySelector(".theme-toggle").click();
    })()`);
    const shoot = async (tag) => {
      const shot = async (name) => {
        await sleep(300);
        await this.screenshot(`${prefix}-${tag}-${name}`);
      };
      for (const tab of ["Wallet", "Private", "Settings"]) {
        await this.tab(tab);
        await shot(tab.toLowerCase());
      }
      await this.tab("Wallet");
      await this.press("Send");
      await shot("send");
      await this.eval("document.querySelector('dialog[open]').close()");
      await this.tab("Private");
      for (const words of ["Send privately", "Receive", "Make private", "Make transparent"]) {
        await sleep(200);
        await this.press(words);
        await shot(`private-${words.toLowerCase().replace(" ", "-")}`);
        await this.eval("document.querySelector('dialog[open]').close()");
      }
    };
    for (const [tag, width, height, mobile, light] of [
      ["desktop-dark", 1400, 900, false, false],
      ["desktop-light", 1400, 900, false, true],
      ["phone-dark", 390, 844, true, false],
      ["phone-light", 390, 844, true, true],
    ]) {
      await design(light);
      await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
      await shoot(tag);
    }
    await design(false);
    await this.send("Emulation.clearDeviceMetricsOverride");
  }

  async enablePrivate() {
    await this.tab("Settings");
    await this.until(`(() => { const box = [...document.querySelectorAll('label.check')].find((node) => node.textContent.includes('Use private transfers'))?.querySelector('input'); return Boolean(box) && !box.disabled; })()`, 60, "the private switch");
    await this.eval(`[...document.querySelectorAll('label.check')].find((node) => node.textContent.includes('Use private transfers')).querySelector('input').click()`);
    await this.tab("Private");
  }

  /** Publishes the private account; the wallet pays its fee from public coins, up to `maximum` TRAI. With `review`
   * milliseconds the review stays open as long, as a person reads it while the chain moves on. */
  async publish(maximum = "1", { review = 0 } = {}) {
    await this.type("Maximum registration fee (TRAI)", maximum);
    await this.press("Review registration");
    await this.until(this.says("Pay fee and publish"), 60, "the registration's review");
    await this.screenshot(`${this.name}-registration-review`);
    await sleep(review);
    await this.type("Password", PASSWORD);
    await this.press("Pay fee and publish", 10);
  }

  async disablePrivate() {
    await this.tab("Settings");
    await this.eval(`[...document.querySelectorAll('label.check')].find((node) => node.textContent.includes('Use private transfers')).querySelector('input').click()`);
    await this.until(`![...document.querySelectorAll('.tabs button')].some((button) => button.textContent.includes('Private'))`, 15, "the Private tab to go");
  }

  async heroText() {
    return this.eval("document.querySelector('.hero__amount')?.textContent ?? ''");
  }

  async privateBalance(atoms, seconds = 150) {
    await this.awake();
    await this.tab("Private");
    const expected = trai(atoms);
    const end = Date.now() + seconds * 1000;
    for (;;) {
      if (await this.eval("!document.querySelector('.tabs')")) {
        await this.awake();
        await this.tab("Private");
      }
      if ((await this.heroText()).startsWith(expected)) return;
      if (Date.now() > end) throw new Error(`${this.name}: the private balance reads ${await this.heroText()}, expected ${expected}; ${await this.notices()}`);
      const button = this.button("Check again");
      await this.eval(`${button}?.click()`);
      await sleep(1000);
    }
  }

  /** From the form to the review, and on to the end of the proof. */
  async startProof() {
    await this.until(this.says("The proof is made on this device"), 90, "the review");
    await this.screenshot(`${this.name}-review`);
    if (await this.eval(`Boolean([...document.querySelectorAll('dialog[open] input[type=password]')].length)`)) await this.type("Password", PASSWORD);
    await this.press("Make the proof and send", 10);
    await this.until(this.says("Proof on this device"), 30, "the proof to start");
    await this.screenshot(`${this.name}-proof`);
  }

  async approve() {
    await this.startProof();
    await this.until(`!(${this.says("Proof on this device")})`, 700, "the proof to end");
  }

  /** A send of `amount` to `address` up to the running proof. */
  async sendPrivately(address, amount) {
    await this.press("Send privately");
    await this.type("Private address of the recipient", address);
    await this.type("Amount (TRAI)", trai(amount));
    await this.press("Review and sign");
    await this.startProof();
  }

  /** An ordinary payment through the send dialog. */
  async pay(address, amount) {
    await this.press("Send");
    await this.type("Recipient", address);
    await this.type("Amount (TRAI)", trai(amount));
    await this.press("Review");
    await this.until(this.says("Recipient fingerprint"), 60, "the review");
    if (await this.eval(this.says("Password"))) await this.type("Password", PASSWORD);
    await this.press("Send");
    await this.until("document.querySelectorAll('dialog[open]').length === 0", 60, "the dialog to close");
  }

  async noneOnWay(seconds = 300) {
    await this.until("document.querySelectorAll('[data-on-way]').length === 0", seconds, "the transaction to settle");
  }
}
