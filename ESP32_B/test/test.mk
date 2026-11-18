# PC tests for ESP32 B. Needs only gcc (or clang) and make.
#   make -f test.mk          build and run the unit tests
#   make -f test.mk sim      build sim_b, ESP32 B on a PC (for host_check_b.py)
#   make -f test.mk clean
CC      ?= gcc
CFLAGS  := -std=gnu11 -Wall -Wextra -Werror -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all
LOGIC   := ../main/station.c ../main/jsonmini.c ../main/tcpframe.c

run: test_b
	./test_b

test_b: test_b.c $(LOGIC) $(wildcard ../main/*.h)
	$(CC) $(CFLAGS) -o $@ test_b.c $(LOGIC)

sim: sim_b

sim_b: sim_b.c ../main/link.c $(LOGIC) $(wildcard ../main/*.h)
	$(CC) $(CFLAGS) -o $@ sim_b.c ../main/link.c $(LOGIC)

clean:
	rm -f test_b sim_b
