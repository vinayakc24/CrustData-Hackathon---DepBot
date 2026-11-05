# toydep 2.0.0

Breaking: the legacy adder `toydep::old_sum(int, int)` is gone in this release; callers
should switch to `toydep::sum(int, int)`, which keeps the same addition behavior.

Performance of `toydep::sum` improved by 10%.
