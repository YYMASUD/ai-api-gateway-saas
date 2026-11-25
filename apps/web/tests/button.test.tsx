import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

describe("Button", () => {
  it("renders a native button with its label", () => {
    render(<Button>Save changes</Button>);
    const button = screen.getByRole("button", { name: "Save changes" });
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBeNull();
  });

  it("forwards the type attribute", () => {
    render(<Button type="submit">Submit</Button>);
    expect(
      screen.getByRole("button", { name: "Submit" }).getAttribute("type"),
    ).toBe("submit");
  });

  it("applies variant and size classes", () => {
    render(
      <Button variant="destructive" size="sm">
        Delete
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Delete" });
    expect(button.className).toContain("bg-destructive");
    expect(button.className).toContain("h-9");
  });

  it("supports asChild so anchors keep their semantics", () => {
    render(
      <Button asChild variant="link">
        <a href="/docs">Docs</a>
      </Button>,
    );
    const link = screen.getByRole("link", { name: "Docs" });
    expect(link.tagName).toBe("A");
    expect(link.className).toContain("underline-offset-4");
  });

  it("exposes the variant helper for external composition", () => {
    expect(buttonVariants({ variant: "outline", size: "lg" })).toContain(
      "border-input",
    );
  });
});

describe("cn", () => {
  it("merges conflicting Tailwind utilities, last one wins", () => {
    expect(cn("px-2 py-1", "px-4")).toBe("py-1 px-4");
  });

  it("ignores falsy values and flattens conditionals", () => {
    expect(cn("text-sm", false && "hidden", undefined, ["font-medium"])).toBe(
      "text-sm font-medium",
    );
  });
});
