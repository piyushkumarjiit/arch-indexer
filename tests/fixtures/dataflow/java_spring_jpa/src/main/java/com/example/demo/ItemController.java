package com.example.demo;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class ItemController {
    private final ItemRepository repository;

    public ItemController(ItemRepository repository) {
        this.repository = repository;
    }

    private String sanitize(String input) {
        return "fixed";
    }

    private String persist(String payload) {
        return repository.save(payload);
    }

    @PostMapping("/items")
    public String createItem(@RequestBody String payload) {
        return persist(payload);
    }

    @PostMapping("/safe")
    public String safeItem(@RequestParam String raw) {
        String cleaned = sanitize(raw);
        return persist(cleaned);
    }
}
