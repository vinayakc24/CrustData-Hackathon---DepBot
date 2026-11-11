#include <geomdep/core.hpp>
#include <geomdep/legacy.hpp>

int main() {
    return geomdep::area(3, 4) + geomdep::perimeter(1, 1) == 16 ? 0 : 1;
}
