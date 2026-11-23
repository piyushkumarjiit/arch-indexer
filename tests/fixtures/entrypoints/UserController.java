package com.example.demo;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.security.access.prepost.PreAuthorize;

@RestController
@RequestMapping("/api/v1")
public class UserController {
    @GetMapping("/users")
    @PreAuthorize("hasRole('USER')")
    public String listUsers() {
        return "users";
    }
}
