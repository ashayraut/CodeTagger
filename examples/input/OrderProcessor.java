package com.example.orders;

import java.math.BigDecimal;
import java.util.List;

/**
 * Processes customer orders from placement through fulfillment.
 * Demonstrates a mix of medium and high risk methods.
 */
public class OrderProcessor {

    private final PaymentGateway paymentGateway;
    private final InventoryService inventoryService;
    private final OrderRepository orderRepository;
    private final NotificationService notificationService;

    public OrderProcessor(PaymentGateway paymentGateway,
                          InventoryService inventoryService,
                          OrderRepository orderRepository,
                          NotificationService notificationService) {
        this.paymentGateway = paymentGateway;
        this.inventoryService = inventoryService;
        this.orderRepository = orderRepository;
        this.notificationService = notificationService;
    }

    /**
     * Places an order: reserves inventory, charges payment, persists the order.
     * Must be idempotent — callers may retry on network failure.
     */
    public Order placeOrder(String customerId, List<OrderItem> items, PaymentDetails payment) {
        inventoryService.reserve(items);
        try {
            PaymentResult result = paymentGateway.charge(payment, totalAmount(items));
            if (!result.isSuccess()) {
                inventoryService.release(items);
                throw new PaymentException("Charge failed: " + result.getErrorCode());
            }
            Order order = Order.create(customerId, items, result.getTransactionId());
            orderRepository.save(order);
            notificationService.sendConfirmation(order);
            return order;
        } catch (RuntimeException e) {
            inventoryService.release(items);
            throw e;
        }
    }

    /** Cancels an order and initiates a refund if payment was captured. */
    public void cancelOrder(String orderId, String reason) {
        Order order = orderRepository.findById(orderId)
                .orElseThrow(() -> new OrderNotFoundException(orderId));

        if (order.getStatus() == OrderStatus.SHIPPED) {
            throw new IllegalStateException("Cannot cancel a shipped order");
        }

        if (order.getStatus() == OrderStatus.PAID) {
            paymentGateway.refund(order.getTransactionId(), order.getTotalAmount());
        }

        inventoryService.release(order.getItems());
        order.cancel(reason);
        orderRepository.save(order);
        notificationService.sendCancellation(order);
    }

    /** Calculates the total price for a list of items including tax. */
    public BigDecimal totalAmount(List<OrderItem> items) {
        return items.stream()
                .map(item -> item.getUnitPrice().multiply(BigDecimal.valueOf(item.getQuantity())))
                .reduce(BigDecimal.ZERO, BigDecimal::add)
                .multiply(BigDecimal.valueOf(1.1)); // 10% tax
    }

    /** Returns a summary string for logging purposes. */
    public String summarize(Order order) {
        return String.format("Order[%s] customer=%s items=%d total=%s status=%s",
                order.getId(), order.getCustomerId(),
                order.getItems().size(), order.getTotalAmount(), order.getStatus());
    }
}
