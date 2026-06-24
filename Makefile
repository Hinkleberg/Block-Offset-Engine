CC = gcc
CFLAGS = -Wall -Wextra -O2 -g -fPIC
LDFLAGS = -luring

# Targets
BIN_TEST_OFFSET = test_offset
BIN_TEST_INTEGRATION = test_integration

# Source files
OFFSET_SRC = offset.c
IO_SRC = io.c
TEST_OFFSET_SRC = test_offset.c
TEST_INTEGRATION_SRC = test_integration.c

# Objects
OFFSET_OBJ = offset.o
IO_OBJ = io.o

.PHONY: all clean test

all: $(BIN_TEST_OFFSET) $(BIN_TEST_INTEGRATION)

$(OFFSET_OBJ): $(OFFSET_SRC)
	$(CC) $(CFLAGS) -c $< -o $@

$(IO_OBJ): $(IO_SRC)
	$(CC) $(CFLAGS) -c $< -o $@

$(BIN_TEST_OFFSET): $(OFFSET_OBJ) $(TEST_OFFSET_SRC)
	$(CC) $(CFLAGS) $(OFFSET_OBJ) $(TEST_OFFSET_SRC) -o $@

$(BIN_TEST_INTEGRATION): $(OFFSET_OBJ) $(IO_OBJ) $(TEST_INTEGRATION_SRC)
	$(CC) $(CFLAGS) $(OFFSET_OBJ) $(IO_OBJ) $(TEST_INTEGRATION_SRC) $(LDFLAGS) -o $@

test: all
	@echo "Running offset calculator tests..."
	./$(BIN_TEST_OFFSET)
	@echo ""
	@echo "Running integration tests (requires O_DIRECT support)..."
	./$(BIN_TEST_INTEGRATION)

clean:
	rm -f *.o $(BIN_TEST_OFFSET) $(BIN_TEST_INTEGRATION) /tmp/engine_test.bin
