// A small service that formats its own type with {fmt}.
// fmt 7.0.0 removed the deprecated alias `fmt::parse_context`
// (changelog: "`*parse_context` - replaced by `*format_parse_context`").
#include <fmt/format.h>

#include <string>

struct Point {
  int x;
  int y;
};

template <> struct fmt::formatter<Point> {
  constexpr auto parse(fmt::parse_context& ctx) { return ctx.begin(); }

  template <typename FormatContext>
  auto format(const Point& p, FormatContext& ctx) {
    return fmt::format_to(ctx.out(), "({}, {})", p.x, p.y);
  }
};

int main() {
  const std::string text = fmt::format("{}", Point{1, 2});
  if (text != "(1, 2)") {
    fmt::print(stderr, "unexpected: {}\n", text);
    return 1;
  }
  fmt::print("{}\n", text);
  return 0;
}
