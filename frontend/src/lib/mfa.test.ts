import { describe, expect, it } from "vitest";
import {
  enrollmentConfirmRequest,
  enrollmentSetupRequest,
  isPlausibleCode,
  normalizeCode,
  MFA_CONFIRM_PATH,
  MFA_SETUP_PATH,
} from "./mfa";

const CREDS = { username: " boss ", password: "S3cure-Passphrase!" };

describe("enrollment steps (SPEC-20-13 reach)", () => {
  it("step 1 posts the credential proof to the setup endpoint", () => {
    const req = enrollmentSetupRequest(CREDS);
    expect(req.path).toBe(MFA_SETUP_PATH);
    expect(req.method).toBe("POST");
    // Username trimmed, password byte-exact; no token is involved because a
    // never-enrolled account cannot hold one.
    expect(req.body).toEqual({
      username: "boss",
      password: "S3cure-Passphrase!",
    });
  });

  it("step 1 omits the re-enrollment code while no device is active", () => {
    expect("code" in enrollmentSetupRequest(CREDS).body).toBe(false);
    expect("code" in enrollmentSetupRequest(CREDS, "").body).toBe(false);
    expect("code" in enrollmentSetupRequest(CREDS, "   ").body).toBe(false);
  });

  it("step 1 sends the code only when one was typed", () => {
    expect(enrollmentSetupRequest(CREDS, "123 456").body.code).toBe("123456");
  });

  it("step 2 posts the same proof plus the code to the confirm endpoint", () => {
    const req = enrollmentConfirmRequest(CREDS, " 654321 ");
    expect(req.path).toBe(MFA_CONFIRM_PATH);
    expect(req.method).toBe("POST");
    expect(req.body).toEqual({
      username: "boss",
      password: "S3cure-Passphrase!",
      code: "654321",
    });
  });

  it("a confirmation without a code still sends the key (server refuses it)", () => {
    // Fail-fast lives in the form; the request shape stays complete so the
    // server is the single authority on whether a code is usable.
    expect(enrollmentConfirmRequest(CREDS, "").body.code).toBe("");
  });
});

describe("code normalisation", () => {
  it("drops the spaces authenticator apps render", () => {
    expect(normalizeCode(" 123 456 ")).toBe("123456");
    expect(normalizeCode(undefined)).toBe("");
  });

  it("accepts exactly six digits and nothing else", () => {
    expect(isPlausibleCode("123456")).toBe(true);
    expect(isPlausibleCode("123 456")).toBe(true);
    expect(isPlausibleCode("12345")).toBe(false);
    expect(isPlausibleCode("1234567")).toBe(false);
    expect(isPlausibleCode("12345a")).toBe(false);
    expect(isPlausibleCode("")).toBe(false);
  });
});