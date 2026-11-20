// frontend/src/test/totp-challenge.test.tsx
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/totp", () => ({
  verifyTotp: vi.fn(async () => ({ token: "real-session-token", principal: {}, memberId: "m1" })),
  sendEmailCode: vi.fn(async () => ({ sentTo: "ada@example.com" })),
  verifyEmailCode: vi.fn(async () => ({
    token: "email-session-token",
    principal: {},
    memberId: "m1",
  })),
}));

import { TotpChallenge } from "@/components/totp-challenge";
import { sendEmailCode, verifyEmailCode, verifyTotp } from "@/lib/totp";

describe("TotpChallenge", () => {
  beforeEach(() => {
    vi.mocked(verifyTotp).mockClear();
    vi.mocked(sendEmailCode).mockClear();
    vi.mocked(verifyEmailCode).mockClear();
  });

  it("submits the code against the challenge token and reports the resulting session", async () => {
    const onVerified = vi.fn();
    render(<TotpChallenge token="challenge-token" onVerified={onVerified} />);
    fireEvent.change(screen.getByLabelText(/code/i), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));
    await waitFor(() => expect(onVerified).toHaveBeenCalledWith("real-session-token"));
    expect(verifyTotp).toHaveBeenCalledWith("challenge-token", "123456");
  });

  it("lets a member with two methods pick email, send a code and verify it", async () => {
    const onVerified = vi.fn();
    render(
      <TotpChallenge token="challenge-token" methods={["totp", "email"]} onVerified={onVerified} />,
    );

    expect(screen.getByRole("button", { name: /authenticator app/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /code by email/i }));
    fireEvent.click(screen.getByRole("button", { name: /^send code$/i }));

    expect(await screen.findByText(/ada@example\.com/)).toBeInTheDocument();
    expect(sendEmailCode).toHaveBeenCalledWith("challenge-token");

    fireEvent.change(screen.getByLabelText(/^code$/i), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith("email-session-token"));
    expect(verifyEmailCode).toHaveBeenCalledWith("challenge-token", "654321");
    expect(verifyTotp).not.toHaveBeenCalled();
  });

  it("offers no choice when the member has a single method", () => {
    render(<TotpChallenge token="challenge-token" methods={["email"]} onVerified={vi.fn()} />);

    expect(screen.queryByRole("button", { name: /authenticator app/i })).toBeNull();
    expect(screen.getByRole("button", { name: /^send code$/i })).toBeInTheDocument();
  });

  it("keeps backup codes available when email is the method", async () => {
    const onVerified = vi.fn();
    render(<TotpChallenge token="challenge-token" methods={["email"]} onVerified={onVerified} />);

    fireEvent.click(screen.getByRole("button", { name: /use a backup code/i }));
    fireEvent.change(screen.getByLabelText(/backup code/i), { target: { value: "ABCDE-12345" } });
    fireEvent.click(screen.getByRole("button", { name: /verify/i }));

    await waitFor(() => expect(onVerified).toHaveBeenCalledWith("real-session-token"));
    expect(verifyTotp).toHaveBeenCalledWith("challenge-token", "ABCDE-12345");
  });

  describe("email resend", () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it("keeps send again disabled for 60 seconds after a send", async () => {
      render(<TotpChallenge token="challenge-token" methods={["email"]} onVerified={vi.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: /^send code$/i }));

      const again = await screen.findByRole("button", { name: /send again/i });
      expect(again).toBeDisabled();

      act(() => {
        vi.advanceTimersByTime(30_000);
      });
      expect(screen.getByRole("button", { name: /send again/i })).toBeDisabled();

      act(() => {
        vi.advanceTimersByTime(31_000);
      });
      expect(screen.getByRole("button", { name: /send again/i })).toBeEnabled();

      fireEvent.click(screen.getByRole("button", { name: /send again/i }));
      await waitFor(() => expect(sendEmailCode).toHaveBeenCalledTimes(2));
    });
  });
});
