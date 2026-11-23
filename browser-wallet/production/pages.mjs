import { WalletPage, PASSWORD, trai } from "../wallet-page.mjs";
import { ReferencePage } from "../reference-page.mjs";

export class ProductionWalletPage extends WalletPage {
  async publishPaid() {
    await this.until(this.visible("private-registration-max-fee"), 60, "the explicit registration fee cap");
    await this.fill("private-registration-max-fee", "2");
    await this.publish(); await this.approve();
    await this.until("!document.getElementById('private-open-enter').disabled", 180, "QC-verified funded registration");
  }
  async cancelActualProof(amount) {
    await this.enter(amount); await this.approve(); await this.until(this.visible("private-proof"), 90, "the actual proof worker");
    await this.click("private-proof-cancel"); await this.until(`!${this.visible("private-proof")}`, 30, "the cancelled worker to end"); await this.noneOnWay();
  }
  async makePrivate(amount) { await this.enter(amount); await this.approve(); await this.proofRuns(900); await this.noneOnWay(); }
  async payPrivate(amount, address) { await this.transfer(amount, address); await this.approve(); await this.proofRuns(900); await this.noneOnWay(); }
  async privateAddress() { return this.text("private-address"); }
  async makeTransparent(address) { await this.exit(address); await this.approve(); await this.proofRuns(900); await this.noneOnWay(); }
}

export class ProductionReferencePage extends ReferencePage {
  async restorePrivate(phrase) { await this.restore(phrase); await this.enablePrivate(); }
  async publishPaid() {
    await this.type("Maximum registration fee (TRAI)", "2"); await this.press("Review registration");
    await this.until(this.says("Funding transaction"), 90, "the fee, change and original funding review");
    await this.type("Password", PASSWORD); await this.press("Pay fee and publish");
    await this.until(`(() => { const button = ${this.button("Send privately")}; return Boolean(button) && !button.disabled; })()`, 180, "QC-verified funded registration");
  }
  async cancelActualProof(amount) {
    await this.press("Make private"); await this.type("Amount (TRAI)", trai(amount)); await this.press("Review and sign");
    await this.startProof(); await this.press("Cancel the proof");
    await this.until(`!(${this.says("Proof on this device")})`, 30, "the cancelled worker to end"); await this.noneOnWay();
  }
  async makePrivate(amount) {
    await this.press("Make private"); await this.type("Amount (TRAI)", trai(amount)); await this.press("Review and sign");
    await this.approve(); await this.noneOnWay();
  }
  async payPrivate(amount, address) {
    await this.sendPrivately(address, amount);
    await this.until(`!(${this.says("Proof on this device")})`, 900, "the real proof to end"); await this.noneOnWay();
  }
  async privateAddress() {
    await this.press("Receive"); await this.until("Boolean(document.querySelector('dialog[open] code.address-box'))", 10, "the private receive address");
    const address = await this.eval("document.querySelector('dialog[open] code.address-box').textContent.trim()");
    await this.eval("document.querySelector('dialog[open]').close()"); return address;
  }
  async makeTransparent(address) {
    await this.press("Make transparent"); await this.type("Transparent address", address);
    await this.until("document.querySelectorAll('dialog[open] label.check input').length > 0", 30, "the local note selection");
    await this.eval("document.querySelector('dialog[open] label.check input').click()"); await this.press("Review and sign");
    await this.approve(); await this.noneOnWay();
  }
}
