import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearAccessToken,
  getAccessToken,
  setAccessToken,
} from "@/lib/auth-token";

const STORAGE_KEY = "ai_gateway_access_token";
const FAKE_TOKEN = "fake.jwt.token-for-tests-only";

describe("auth token helper", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it("returns null when nothing is stored", () => {
    expect(getAccessToken()).toBeNull();
  });

  it("stores and reads the token from sessionStorage", () => {
    setAccessToken(FAKE_TOKEN);
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe(FAKE_TOKEN);
    expect(getAccessToken()).toBe(FAKE_TOKEN);
  });

  it("never writes to localStorage", () => {
    setAccessToken(FAKE_TOKEN);
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(window.localStorage.length).toBe(0);
  });

  it("clears the token", () => {
    setAccessToken(FAKE_TOKEN);
    clearAccessToken();
    expect(getAccessToken()).toBeNull();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("is safe when storage throws", () => {
    const thrower = () => {
      throw new Error("storage disabled");
    };
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(thrower);
    const getItemSpy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(thrower);
    const removeItemSpy = vi
      .spyOn(Storage.prototype, "removeItem")
      .mockImplementation(thrower);

    try {
      expect(() => setAccessToken(FAKE_TOKEN)).not.toThrow();
      expect(getAccessToken()).toBeNull();
      expect(() => clearAccessToken()).not.toThrow();
    } finally {
      setItemSpy.mockRestore();
      getItemSpy.mockRestore();
      removeItemSpy.mockRestore();
    }
  });
});
