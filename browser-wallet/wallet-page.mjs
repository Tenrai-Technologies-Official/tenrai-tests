// What a person does in the normal web wallet, for the tests in this folder.

import { Browser, sleep } from "./browser.mjs";

export const ATOMS = 100_000_000n;
export const PASSWORD = "correct horse battery staple";
export const READY = "!document.getElementById('private-open-send').disabled";
export const trai = (atoms) => `${atoms / ATOMS}.${String(atoms % ATOMS).padStart(8, "0")}`;

/** The normal web wallet in a browser or the desktop app: what a person does in it. */
export class WalletPage extends Browser {
  /** The wallet takes a phrase only once it knows its network, which a person never outruns. */
  async connected() {
    await this.until("['connected', 'live', 'syncing'].includes(document.getElementById('link-status').dataset.state)", 60, "the connection");
  }

  async restore(phrase) {
    await this.until(this.visible("setup-panel"), 30, "the setup");
    await this.connected();
    await this.eval(`document.querySelector('[data-setup="restore"]').click()`);
    await this.click("open-restore");
    await this.fill("restore-phrase", phrase);
    await this.fill("restore-password", PASSWORD);
    await this.fill("restore-password-confirm", PASSWORD);
    await this.until("!document.getElementById('restore-submit').disabled", 10, "the phrase to pass");
    await this.click("restore-submit");
    await this.until("!document.getElementById('private-enabled').disabled", 60, "the private switch");
    await this.click("private-enabled");
    await this.privateTab();
  }

  /** Opens the Private tab, which shows while the wallet's switch is on. */
  async privateTab() {
    await this.until("!document.getElementById('private-tab').hidden", 60, "the Private tab");
    await this.eval("document.getElementById('private-tab').click()");
    await this.until(this.visible("private-balance"), 10, "the Private tab to open");
  }

  async unlock() {
    await this.until(this.visible("unlock-panel"), 30, "the unlock page");
    await this.fill("unlock-password", PASSWORD);
    await this.eval("document.getElementById('unlock-form').requestSubmit()");
    await this.privateTab();
  }

  /** A wallet that locked itself after its idle time opens again, as a person coming back unlocks it; a locked page
   * still holds the balances it showed before. */
  async awake() {
    if (await this.eval(this.visible("unlock-panel"))) await this.unlock();
  }

  /** A wallet that was published before, opened in a browser new to it. */
  async openedElsewhere(phrase) {
    await this.restore(phrase);
    await this.until(READY, 120, "the private buttons");
  }

  /** Publishes the private account; the wallet pays its fee from public coins, up to `maximum` TRAI. With `review`
   * milliseconds the review stays open as long, as a person reads it while the chain moves on. */
  async publish(maximum = "1", { review = 0 } = {}) {
    await this.until(this.visible("private-register"), 60, "the publish button");
    await this.until(this.visible("private-registration-max-fee"), 30, "the field of the maximum fee");
    await this.fill("private-registration-max-fee", maximum);
    await this.click("private-register");
    if (review) {
      await this.until("document.getElementById('review-dialog').open", 90, "the review");
      await sleep(review);
    }
    await this.approve();
  }

  /** Waits for the check to end and the balance to read as `atoms`. */
  async privateBalance(atoms, seconds = 120) {
    const expected = trai(atoms);
    const end = Date.now() + seconds * 1000;
    for (;;) {
      await this.awake();
      if (!(await this.eval(this.visible("private-checking")))) {
        const shown = await this.text("private-balance");
        if (shown.startsWith(expected)) return shown;
      }
      if (Date.now() > end) throw new Error(`${this.name}: the private balance reads ${await this.text("private-balance")}, expected ${expected}; ${await this.text("private-problem")} ${await this.notices()}`);
      await sleep(500);
      await this.click("private-refresh");
    }
  }

  async transparentBalance(atoms, seconds = 120) {
    const expected = trai(atoms);
    const end = Date.now() + seconds * 1000;
    for (;;) {
      await this.awake();
      const shown = await this.text("balance-total");
      if (shown.startsWith(expected)) return shown;
      if (Date.now() > end) throw new Error(`${this.name}: the balance reads ${shown}, expected ${expected}; ${await this.notices()}`);
      await sleep(500);
    }
  }

  /** Opens the dialog on one of its ways, as its button in the tab does. */
  async tab(name) {
    await this.eval(`document.getElementById('private-tab').click()`);
    await this.eval(`document.getElementById('private-open-${name}').click()`);
    await this.until(`document.getElementById('private-dialog').open`, 10, "the private dialog");
  }

  /** A dialog left open by a refusal is closed, as a person would. */
  async closeDialog() {
    await this.eval("document.getElementById('private-dialog').close()");
  }

  async onWayAmount() {
    return this.text("private-on-way");
  }

  /** Passes the review dialog with the wallet's password. */
  async approve() {
    await this.until("document.getElementById('review-dialog').open", 90, "the review");
    await this.screenshot(`${this.name}-review`);
    const asked = await this.eval(this.visible("review-password"));
    if (asked) await this.fill("review-password", PASSWORD);
    await this.eval("document.getElementById('review-dialog').querySelector('form').requestSubmit(document.getElementById('review-approve'))");
    await this.until("!document.getElementById('review-dialog').open", 30, "the review to close");
  }

  async enter(amount, to = "") {
    await this.tab("enter");
    await this.fill("private-enter-amount", trai(amount));
    await this.fill("private-enter-to", to);
    await this.eval("document.getElementById('private-enter-form').requestSubmit(document.getElementById('private-enter-submit'))");
  }

  async transfer(amount, to) {
    await this.tab("send");
    await this.fill("private-send-to", to);
    await this.fill("private-send-amount", trai(amount));
    await this.eval("document.getElementById('private-send-form').requestSubmit(document.getElementById('private-send-submit'))");
  }

  async exit(to, count = 1) {
    await this.tab("exit");
    await this.fill("private-exit-to", to);
    await this.eval(`[...document.querySelectorAll('#private-exit-notes input')].slice(0, ${count}).forEach((box) => { if (!box.checked) box.click(); })`);
    await this.eval("document.getElementById('private-exit-form').requestSubmit(document.getElementById('private-exit-submit'))");
  }

  /** Waits while the proof runs and returns how long it took, in seconds. */
  async proofRuns(seconds = 700) {
    const started = Date.now();
    await this.until(this.visible("private-proof"), 90, "the proof to start");
    await this.screenshot(`${this.name}-proof`);
    await this.until(`!${this.visible("private-proof")}`, seconds, "the proof to end");
    return (Date.now() - started) / 1000;
  }

  /** Waits until no transaction is on its way. */
  async noneOnWay(seconds = 300) {
    await this.until("document.querySelectorAll('#private-activity [data-on-way]').length === 0", seconds, "the transaction to settle");
  }

  /** The page in the sizes, designs and languages people use, for a look by eye. */
  async gallery(prefix, { proof = false } = {}) {
    const language = async (code) => {
      await this.eval("document.querySelector('[data-tab=\"settings\"]').click()");
      await this.eval("document.querySelector('#settings-language .lang__button').click()");
      await this.eval(`document.querySelector('#settings-language [data-code="${code}"]').click()`);
      await sleep(600);
    };
    const design = (light) => this.eval(`(() => {
      if ((document.documentElement.dataset.theme === "light") !== ${light}) document.querySelector("button[data-theme-toggle]").click();
    })()`);
    const shoot = async (tag) => {
      const shot = async (name) => {
        await sleep(250);
        await this.screenshot(`${prefix}-${tag}-${name}`);
      };
      for (const tab of ["wallet", "private", "settings"]) {
        await this.eval(`document.querySelector('[data-tab="${tab}"]').click()`);
        await shot(tab);
      }
      await this.eval("document.querySelector('[data-tab=\"wallet\"]').click()");
      await this.click("open-send");
      await shot("send");
      await this.eval("document.getElementById('send-dialog').close()");
      for (const name of ["send", "enter", "exit"]) {
        await this.tab(name);
        await shot(`private-${name}`);
        await this.closeDialog();
      }
      await this.click("private-open-receive");
      await shot("private-receive");
      await this.eval("document.getElementById('private-receive-dialog').close()");
    };
    const size = (width, height, mobile) => this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile });
    const sets = [
      ["desktop-dark-en", 1400, 900, false, false, "en"],
      ["desktop-light-en", 1400, 900, false, true, "en"],
      ["phone-dark-en", 390, 844, true, false, "en"],
      ["phone-light-de", 390, 844, true, true, "de"],
      ["desktop-dark-de", 1400, 900, false, false, "de"],
    ];
    for (const [tag, width, height, mobile, light, code] of sets) {
      await language(code);
      await design(light);
      await size(width, height, mobile);
      await shoot(tag);
    }
    await design(false);
    await language("en");
    if (proof) {
      await size(390, 844, true);
      await this.enter(ATOMS);
      await this.approve();
      await this.until(this.visible("private-proof"), 90, "the proof to start");
      await sleep(1500);
      await this.screenshot(`${prefix}-phone-proof`);
      await this.click("private-proof-cancel");
      await this.until(`!${this.visible("private-proof")}`, 30, "the proof to end");
    }
    await this.send("Emulation.clearDeviceMetricsOverride");
  }

  async onWay() {
    return this.eval("document.querySelectorAll('#private-activity [data-on-way]').length");
  }
}
